"""Contest access views omit service-master events without deleting history."""
from uuid import uuid4

import pytest

from app.orm_models import AccessLogRow, StaffAccountRow
from app.services.store import store
from test_operator_management_improvements import context, client, login
from test_performance_controls import cache


@pytest.fixture
def history(context):
    c = context
    visited = client.get(c["prefix"] + "/dashboard", headers=c["viewer"])
    assert visited.status_code == 200, visited.text
    initial = client.get(c["prefix"] + "/access-logs", headers=c["viewer"])
    assert initial.status_code == 200, initial.text
    visible = {row["access_log_id"] for row in initial.json()["data"]}
    assert len(visible) == 1  # The viewer's own ordinary operator visit.
    marker = uuid4().hex
    master_only = f"master-only-{marker}@example.com"
    current_master = "test3@zoj.com"
    events = [
        ("service_master", "staff", "operator_access", master_only),
        ("operator", "staff", "operator_access", current_master),
        ("service_master", "general", "login_failed", f"hidden-failure-{marker}@example.com"),
        ("participant", "participant", "participant_login", current_master),
        ("service_master", "staff", "operator_access", current_master),
        (None, "staff", "login_failed", current_master),
        ("", "participant", "session_conflict", f"legacy-{marker}@example.com"),
        ("master", "staff", "operator_access", c["email"]),
        ("service_master", "general", "session_conflict", f"former-master-{marker}@example.com"),
        ("unknown", "general", "password_changed", f"unknown-{marker}@example.com"),
    ]
    hidden, all_ids = set(), set(visible)
    for role, scope, event, email in events:
        row = store.append_access_log(contest_id=c["cid"], actor_role=role,
            account_scope=scope, event_type=event, email=email)
        all_ids.add(row.access_log_id)
        (hidden if role == "service_master" else visible).add(row.access_log_id)
    return {**c, "visible": visible, "hidden": hidden, "all_ids": all_ids,
        "master_only": master_only, "master": login(current_master)}


def test_contest_pages_count_and_search_only_the_visible_events(history):
    c = history
    cursor, found = None, []
    while True:
        params = {"limit": 2, "exclude_service_master": "false"}
        if cursor:
            params["cursor"] = cursor
        response = client.get(c["prefix"] + "/access-logs", headers=c["viewer"], params=params)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["page"]["total_count"] == len(c["visible"])
        assert 0 < len(data["data"]) <= 2
        assert all(row["actor_role"] != "service_master" for row in data["data"])
        found.extend(row["access_log_id"] for row in data["data"])
        cursor = data["page"]["next_cursor"]
        if cursor is None:
            break
    assert len(found) == len(set(found)) and set(found) == c["visible"]
    searched = client.get(c["prefix"] + "/access-logs", headers=c["viewer"],
        params={"email": c["master_only"], "limit": 1}).json()
    assert searched["data"] == [] and searched["page"]["total_count"] == 0
    assert searched["page"]["next_cursor"] is None
    kept = client.get(c["prefix"] + "/access-logs", headers=c["viewer"],
        params={"email": "TEST3@ZOJ.COM"}).json()
    assert kept["page"]["total_count"] == 3
    assert {row["actor_role"] for row in kept["data"]} == {"operator", "participant", None}


def test_service_masters_get_the_same_contest_projection_but_admin_keeps_originals(history):
    c = history
    response = client.get(c["prefix"] + "/access-logs", headers=c["master"])
    assert response.status_code == 200, response.text
    assert {row["access_log_id"] for row in response.json()["data"]} == c["visible"]
    admin = client.get("/api/admin/access-logs", headers=c["master"], params={"contest_id": c["cid"]})
    assert admin.status_code == 200, admin.text
    assert c["all_ids"].issubset(row["access_log_id"] for row in admin.json()["data"])
    assert c["hidden"].issubset(row["access_log_id"] for row in admin.json()["data"])
    raw, _, count = store.list_access_logs(contest_id=c["cid"])
    assert c["all_ids"].issubset(row.access_log_id for row in raw) and count >= len(c["all_ids"])
    with store._session() as db:
        assert all(db.get(AccessLogRow, identity) is not None for identity in c["all_ids"])


@pytest.mark.parametrize("first", ["admin", "operator"])
def test_cached_stats_keep_admin_and_contest_audiences_separate(history, cache, first):
    c = history
    def stats(audience):
        path = "/api/admin/access-log-stats" if audience == "admin" else c["prefix"] + "/access-log-stats"
        response = client.get(path, headers=c["master"] if audience == "admin" else c["viewer"],
            params={"contest_id": c["cid"], "exclude_service_master": "false"})
        assert response.status_code == 200, response.text
        return response.json()["data"]
    stats(first)
    visible, raw = stats("operator"), stats("admin")
    assert visible["total_count"] == 7 and raw["total_count"] == 11
    assert (visible["success_count"], visible["failed_count"], visible["conflict_count"], visible["unique_account_count"]) == (4, 1, 1, 5)
    assert (raw["success_count"], raw["failed_count"], raw["conflict_count"], raw["unique_account_count"]) == (6, 2, 2, 8)
    store.append_access_log(contest_id=c["cid"], event_type="operator_access", account_scope="staff",
        email=f"new-hidden-{uuid4().hex}@example.com", actor_role="service_master")
    assert stats("operator") == visible
    assert stats("admin")["total_count"] == raw["total_count"] + 1
    store.append_access_log(contest_id=c["cid"], event_type="participant_login", account_scope="participant",
        email=f"new-visible-{uuid4().hex}@example.com", actor_role="participant")
    changed = stats("operator")
    assert changed["total_count"] == visible["total_count"] + 1
    assert changed["unique_account_count"] == visible["unique_account_count"] + 1


def test_recorded_role_is_stable_after_account_promotion_and_demotion(history):
    c = history
    account = store.upsert_contest_operator(c["cid"], f"changing-{uuid4().hex}@example.com", "Changing role", ["audit_viewer"])
    operator = store.append_access_log(contest_id=c["cid"], event_type="operator_access", account_scope="staff",
        email=str(account.email), actor_role="operator")
    master = store.append_access_log(contest_id=c["cid"], event_type="operator_access", account_scope="staff",
        email=str(account.email), actor_role="service_master")
    for is_master in (True, False):
        with store._session() as db:
            db.get(StaffAccountRow, account.staff_account_id).is_service_master = is_master
            db.commit()
        response = client.get(c["prefix"] + "/access-logs", headers=c["viewer"], params={"email": str(account.email)})
        assert response.status_code == 200
        assert [row["access_log_id"] for row in response.json()["data"]] == [operator.access_log_id]
        assert store.list_access_logs(contest_id=c["cid"], email=str(account.email))[2] == 2
        with store._session() as db:
            assert db.get(AccessLogRow, master.access_log_id).actor_role == "service_master"


@pytest.mark.parametrize("suffix", ["/access-logs", "/access-log-stats"])
def test_access_log_permission_gate_is_unchanged(history, suffix):
    c = history
    assert client.get(c["prefix"] + suffix).status_code == 401
    assert client.get(c["prefix"] + suffix, headers=c["manager"]).status_code == 200
    outsider = store.upsert_contest_operator(store.create_contest("Other contest", "Host", "").contest_id,
        f"outsider-{uuid4().hex}@example.com", "Outsider", ["master"])
    assert client.get(c["prefix"] + suffix, headers=login(str(outsider.email))).status_code == 403
    assert client.get("/api/admin" + suffix, headers=c["viewer"]).status_code == 403
