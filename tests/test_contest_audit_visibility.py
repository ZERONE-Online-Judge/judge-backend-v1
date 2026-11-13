"""Contest operation views omit master activity while retaining raw history."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.orm_models import OperationalAuditLogRow, StaffAccountRow
from app.services.store import store
from test_operator_management_improvements import context, client, login
from test_editor_bridge import (
    config as bridge_config,
    context as archive_context,
    client as bridge_client,
    export as editor_package,
    headers as signed_headers,
    target as import_target,
)


@pytest.fixture
def audit_history(context):
    c = context
    marker = uuid4().hex
    master = "test3@zoj.com"
    hidden_only = f"hidden-audit-{marker}@example.com"
    ordinary = store.upsert_contest_operator(
        c["cid"], f"ordinary-master-{marker}@example.com", "대회 마스터", ["master"]
    )
    # Current account roles cannot replace explicitly recorded historical roles.
    # Korean '마스터' alone was also used for contest masters by Editor imports.
    entries = [
        ("service_master", hidden_only, "operator", 200, True),
        ("service_master", master, "operator", 403, True),
        ("service_master", f"former-{marker}@example.com", "admin", 200, True),
        ("마스터", master, "operator", 200, True),
        ("마스터", f"  {master.upper()}  ", "operator", 200, True),
        ("operator", master, "operator", 200, False),
        (None, master, "operator", 403, False),
        ("", f"blank-{marker}@example.com", "operator", 200, False),
        ("master", str(ordinary.email), "operator", 200, False),
        ("마스터", str(ordinary.email), "operator", 200, False),
        ("총괄", c["email"], "operator", 200, False),
        ("출제진", f"author-{marker}@example.com", "operator", 200, False),
        ("검수진", f"reviewer-{marker}@example.com", "operator", 200, False),
        ("운영진", f"staff-{marker}@example.com", "operator", 200, False),
        ("unknown", f"unknown-{marker}@example.com", "admin", 200, False),
        ("마스터", f"missing-legacy-{marker}@example.com", "operator", 200, False),
        ("마스터", None, "operator", 200, False),
    ]
    visible, hidden, all_ids = set(), set(), set()
    for index, (role, email, scope, status, omit) in enumerate(entries):
        row = store.append_operational_audit_log(
            scope=scope, action=f"독립 작업 {index}", method="PATCH",
            path=f"{c['prefix']}/synthetic-target/{index}", status_code=status,
            actor_email=email, actor_role=role, contest_id=c["cid"],
            details={"synthetic_marker": marker, "saved_value": index},
        )
        all_ids.add(row.operational_audit_log_id)
        (hidden if omit else visible).add(row.operational_audit_log_id)
    other = store.create_contest("다른 대회의 작업", "ZOJ", "")
    foreign = store.append_operational_audit_log(
        scope="operator", action="다른 대회의 작업", method="PATCH",
        path=f"/api/operator/contests/{other.contest_id}/settings", status_code=200,
        actor_email=c["email"], actor_role="operator", contest_id=other.contest_id,
    )
    return {
        **c, "visible": visible, "hidden": hidden, "all_ids": all_ids,
        "hidden_only": hidden_only, "master": login(master), "ordinary": ordinary,
        "foreign_id": foreign.operational_audit_log_id,
    }


def test_contest_audit_pages_count_search_and_paginate_only_visible_history(audit_history):
    c = audit_history
    cursor, found = None, []
    while True:
        params = {"limit": 2, "exclude_service_master": "false"}
        if cursor:
            params["cursor"] = cursor
        response = client.get(c["prefix"] + "/audit-logs", headers=c["viewer"], params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["page"]["total_count"] == len(c["visible"])
        assert 0 < len(result["data"]) <= 2
        found.extend(item["operational_audit_log_id"] for item in result["data"])
        cursor = result["page"]["next_cursor"]
        if cursor is None:
            break
    assert len(found) == len(set(found))
    assert set(found) == c["visible"]
    assert not c["hidden"].intersection(found)
    assert c["foreign_id"] not in found

    hidden = client.get(
        c["prefix"] + "/audit-logs", headers=c["viewer"],
        params={"actor_email": c["hidden_only"].upper(), "limit": 1},
    ).json()
    assert hidden["data"] == []
    assert hidden["page"]["total_count"] == 0
    assert hidden["page"]["next_cursor"] is None
    # The same account's ordinary/unknown-role historical events remain visible.
    mixed = client.get(
        c["prefix"] + "/audit-logs", headers=c["viewer"],
        params={"actor_email": "TEST3@ZOJ.COM"},
    ).json()
    assert mixed["page"]["total_count"] == 2
    assert {item["actor_role"] for item in mixed["data"]} == {"operator", None}


def test_contest_master_projection_is_identical_but_admin_retains_original_history(audit_history):
    c = audit_history
    for viewer in (c["viewer"], c["owner"], c["master"]):
        result = client.get(c["prefix"] + "/audit-logs", headers=viewer)
        assert result.status_code == 200, result.text
        assert {item["operational_audit_log_id"] for item in result.json()["data"]} == c["visible"]

    admin = client.get(
        "/api/admin/audit-logs", headers=c["master"],
        params={"contest_id": c["cid"], "exclude_service_master": "true"},
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["page"]["total_count"] == len(c["all_ids"])
    assert {item["operational_audit_log_id"] for item in admin.json()["data"]} == c["all_ids"]
    assert c["hidden"].issubset(item["operational_audit_log_id"] for item in admin.json()["data"])
    for scope in ("operator", "admin"):
        result = client.get(
            "/api/admin/audit-logs", headers=c["master"],
            params={"contest_id": c["cid"], "scope": scope},
        )
        assert result.status_code == 200
        assert all(item["scope"] == scope for item in result.json()["data"])
    raw, _, count = store.list_operational_audit_logs(contest_id=c["cid"])
    assert count == len(c["all_ids"])
    assert {item.operational_audit_log_id for item in raw} == c["all_ids"]
    with store._session() as db:
        assert all(db.get(OperationalAuditLogRow, identity) is not None for identity in c["all_ids"])


def test_legacy_master_filter_keeps_ordinary_contest_masters_and_unknown_identity(audit_history):
    c = audit_history
    ordinary = client.get(
        c["prefix"] + "/audit-logs", headers=c["viewer"],
        params={"actor_email": str(c["ordinary"].email)},
    ).json()
    assert ordinary["page"]["total_count"] == 2
    assert {item["actor_role"] for item in ordinary["data"]} == {"master", "마스터"}
    # A missing identity is ambiguous, so it must not hide a contest master's work.
    result = client.get(c["prefix"] + "/audit-logs", headers=c["viewer"]).json()
    assert any(item["actor_role"] == "마스터" and item["actor_email"] is None for item in result["data"])


def test_canonical_recorded_role_does_not_change_on_account_promotion_or_demotion(context):
    c = context
    account = store.upsert_contest_operator(
        c["cid"], f"changing-audit-{uuid4().hex}@example.com", "역할 변경 계정", ["audit_viewer"]
    )
    rows = {}
    for role in ("operator", "service_master", None, ""):
        rows[role] = store.append_operational_audit_log(
            scope="operator", action="당시 역할 보존", method="PATCH", path=c["prefix"] + "/settings",
            status_code=200, actor_email=str(account.email), actor_role=role, contest_id=c["cid"],
        ).operational_audit_log_id
    try:
        for is_master in (True, False):
            with store._session() as db:
                db.get(StaffAccountRow, account.staff_account_id).is_service_master = is_master
                db.commit()
            response = client.get(
                c["prefix"] + "/audit-logs", headers=c["viewer"],
                params={"actor_email": str(account.email)},
            )
            assert response.status_code == 200, response.text
            assert {item["operational_audit_log_id"] for item in response.json()["data"]} == {
                rows["operator"], rows[None], rows[""],
            }
            assert store.list_operational_audit_logs(contest_id=c["cid"], actor_email=str(account.email))[2] == 4
    finally:
        with store._session() as db:
            db.get(StaffAccountRow, account.staff_account_id).is_service_master = False
            db.commit()


def test_contest_audit_permission_gates_remain_unchanged(audit_history):
    c = audit_history
    path = c["prefix"] + "/audit-logs"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=c["manager"]).status_code == 403
    assert client.get(path, headers=c["viewer"]).status_code == 200
    other = store.create_contest("권한 없는 다른 대회", "ZOJ", "")
    outsider = store.upsert_contest_operator(
        other.contest_id, f"outside-audit-{uuid4().hex}@example.com", "다른 대회 운영자", ["master"]
    )
    assert client.get(path, headers=login(str(outsider.email))).status_code == 403
    assert client.get("/api/admin/audit-logs", headers=c["viewer"]).status_code == 403


def test_editor_import_writer_uses_service_identity_without_hiding_contest_master(archive_context, bridge_config):
    c = archive_context
    ordinary = store.upsert_contest_operator(
        c["cid"], f"import-master-{uuid4().hex}@example.com", "대회 마스터", ["master"]
    )
    content = editor_package(c)
    imported = {}
    for code, email in (("S", "test3@zoj.com"), ("M", str(ordinary.email))):
        path = import_target(c, code=code)
        response = bridge_client.post(path, content=content, headers=signed_headers("POST", path, email, content))
        assert response.status_code == 200, response.text
        imported[email] = response.json()["data"]["problem_id"]
    with store._session() as db:
        rows = list(db.scalars(select(OperationalAuditLogRow).where(
            OperationalAuditLogRow.contest_id == c["cid"],
            OperationalAuditLogRow.action == "Editor 문제 가져오기",
        )))
        assert len(rows) == 2
        assert next(row for row in rows if row.actor_email == "test3@zoj.com").actor_role == "service_master"
        assert next(row for row in rows if row.actor_email == str(ordinary.email)).actor_role == "마스터"
        hidden = next(row.operational_audit_log_id for row in rows if row.actor_email == "test3@zoj.com")
        visible = next(row.operational_audit_log_id for row in rows if row.actor_email == str(ordinary.email))
    response = client.get(c["prefix"] + "/audit-logs", headers=c["owner"])
    assert response.status_code == 200, response.text
    identities = {item["operational_audit_log_id"] for item in response.json()["data"]}
    assert visible in identities
    assert hidden not in identities
