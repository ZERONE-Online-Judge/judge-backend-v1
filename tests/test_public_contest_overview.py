"""Anonymous contest overviews and independent post-contest resource policies."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestResourceAccess, ContestStatus, now_utc
from app.services.store import OPERATOR_TEST_TEAM_PREFIX, store
from tests.contest_fixtures import set_contest_schedule

client = TestClient(app)
RESOURCE_PATHS = {
    "problem": "/problems",
    "scoreboard": "/scoreboard",
    "submission": "/submissions",
    "board": "/boards",
    "notice": "/notices",
}


@pytest.fixture
def contest_context():
    now = now_utc()
    contest = store.create_contest(
        "Public overview test", "Contest host", "Public contest description",
        start_at=now - timedelta(hours=4), end_at=now - timedelta(hours=1),
        freeze_at=now - timedelta(hours=2), status=ContestStatus.ENDED,
    )
    cid = contest.contest_id
    store.upsert_contest_operator(cid, f"owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    divisions = [store.create_contest_division(cid, code, code) for code in ["junior", "senior"]]
    problem = store.create_problem(cid, divisions[0].division_id, "A", "Public problem", "Statement", 1000, 128, {}, 1)
    store.update_problem(cid, problem.problem_id, editorial="Participant-only editorial")
    email = f"participant-{uuid4().hex}@zoj.com"
    team = store.create_participant_team(
        cid, divisions[0].division_id, "Private team name", "Private member name", email,
        [("Another private member", f"member-{uuid4().hex}@zoj.com")],
    )
    store.create_participant_team(cid, divisions[1].division_id, "Other private team", "Other member", f"other-{uuid4().hex}@zoj.com", [])
    store.update_contest_settings(
        cid, **{f"{resource}_access_after_end": ContestResourceAccess.PARTICIPANTS for resource in RESOURCE_PATHS},
        editorial_access_after_end=ContestResourceAccess.PARTICIPANTS,
        emergency_notice="Participant-only emergency notice",
    )
    return {"cid": cid, "base": f"/api/contests/{cid}", "detail": f"/api/public/contests/{cid}", "divisions": divisions, "problem": problem, "team": team, "email": email}


def participant_headers(c):
    response = client.post(c["base"] + "/participant-login/otp/verify", json={"email": c["email"], "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


def general_headers(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


@pytest.mark.parametrize("status", [ContestStatus.OPEN, ContestStatus.RUNNING, ContestStatus.ENDED])
def test_visible_overview_has_safe_registration_counts_without_participant_access(contest_context, status):
    c = contest_context
    now = now_utc()
    if status == ContestStatus.OPEN:
        start, end = now + timedelta(days=1), now + timedelta(days=1, hours=3)
    elif status == ContestStatus.RUNNING:
        start, end = now - timedelta(hours=1), now + timedelta(hours=2)
    else:
        start, end = now - timedelta(hours=4), now - timedelta(hours=1)
    set_contest_schedule(c["cid"], status=status, start_at=start, end_at=end, freeze_at=end - timedelta(hours=1))
    # Legacy operator test teams and virtual previews are not contestants.
    store.create_participant_team(c["cid"], c["divisions"][0].division_id, OPERATOR_TEST_TEAM_PREFIX + uuid4().hex, "Operator test", f"test-{uuid4().hex}@zoj.com", [])
    preview = store.upsert_contest_operator(c["cid"], f"preview-{uuid4().hex}@zoj.com", "Preview staff", ["participant_preview"])
    preview_session = general_headers(str(preview.email))
    selected = client.post(f"/api/auth/general/contests/{c['cid']}/participant-preview-session", headers=preview_session, json={"division_id": c["divisions"][0].division_id})
    assert selected.status_code == 200

    response = client.get(c["detail"])
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["participant_count"] == 3
    assert data["team_count"] == 2
    assert data["contest"]["title"] == "Public overview test"
    assert data["contest"]["organization_name"] == "Contest host"
    assert data["contest"]["overview"] == "Public contest description"
    assert data["contest"]["status"] == status.value
    assert len(data["divisions"]) == 2
    for confidential in [c["email"], c["team"].team_name, c["team"].participant_team_id, "Private member name", "Preview staff", str(preview.email)]:
        assert confidential not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "Authorization" in response.headers["vary"]


def test_empty_visible_contest_has_zero_registration_counts():
    contest = store.create_contest("Empty overview", "Host", "Overview", status=ContestStatus.OPEN)
    response = client.get(f"/api/public/contests/{contest.contest_id}")
    assert response.status_code == 200
    assert response.json()["data"]["participant_count"] == 0
    assert response.json()["data"]["team_count"] == 0


@pytest.mark.parametrize("status", [ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD, ContestStatus.SCHEDULED])
def test_hidden_contest_does_not_expose_overview_or_counts(contest_context, status):
    c = contest_context
    start = now_utc() + timedelta(days=1)
    set_contest_schedule(c["cid"], status=status, start_at=start, end_at=start + timedelta(hours=3), freeze_at=start + timedelta(hours=2))
    assert client.get(c["detail"]).status_code == 404
    assert c["cid"] not in {item["contest_id"] for item in client.get("/api/public/contests").json()["data"]}


@pytest.mark.parametrize("restricted", [ContestResourceAccess.PARTICIPANTS, ContestResourceAccess.PRIVATE])
@pytest.mark.parametrize("public_resource", RESOURCE_PATHS)
def test_each_public_resource_is_accessible_independently(contest_context, public_resource, restricted):
    c = contest_context
    policies = {f"{resource}_access_after_end": restricted for resource in RESOURCE_PATHS}
    policies[f"{public_resource}_access_after_end"] = ContestResourceAccess.PUBLIC
    store.update_contest_settings(c["cid"], **policies)

    assert client.get(c["detail"]).status_code == 200
    for resource, path in RESOURCE_PATHS.items():
        response = client.get(c["base"] + path)
        assert response.status_code == (200 if resource == public_resource else 404), (resource, response.text)
    problem_detail = client.get(c["base"] + f"/problems/{c['problem'].problem_id}")
    workspace = client.get(c["base"] + "/workspace")
    division_workspace = client.get(c["base"] + f"/divisions/{c['divisions'][0].division_id}/workspace")
    if public_resource == "problem":
        assert problem_detail.status_code == workspace.status_code == division_workspace.status_code == 200
        assert problem_detail.json()["data"]["editorial"] == ""
        assert workspace.json()["data"]["problems"][0]["problem_id"] == c["problem"].problem_id
        assert workspace.json()["data"]["emergency_notice"] is None
    else:
        assert problem_detail.status_code == workspace.status_code == division_workspace.status_code == 404


@pytest.mark.parametrize("access", [ContestResourceAccess.PARTICIPANTS, ContestResourceAccess.PRIVATE])
def test_restricted_emergency_notice_stays_private_in_overview_and_public_workspace(contest_context, access):
    c = contest_context
    template = "Participant-only emergency notice {{countdown:end}}"
    store.update_contest_settings(c["cid"], emergency_notice=template)
    expected = "Participant-only emergency notice 대회가 종료되었습니다. 수고하셨습니다."
    store.update_contest_settings(c["cid"], problem_access_after_end=ContestResourceAccess.PUBLIC, notice_access_after_end=access)
    outsider_account = store.upsert_contest_operator(c["cid"], f"outsider-{uuid4().hex}@zoj.com", "Reviewer", ["problem_reviewer"])
    outsider = general_headers(str(outsider_account.email))
    for headers in [{}, {"Authorization": "Bearer invalid"}, outsider]:
        detail = client.get(c["detail"], headers=headers)
        listing = client.get("/api/public/contests", headers=headers)
        assert detail.json()["data"]["contest"]["emergency_notice"] is None
        assert detail.json()["data"]["contest"]["emergency_notice_template"] is None
        assert next(item for item in listing.json()["data"] if item["contest_id"] == c["cid"])["emergency_notice"] is None
        assert next(item for item in listing.json()["data"] if item["contest_id"] == c["cid"])["emergency_notice_template"] is None
        for path in ["/workspace", f"/divisions/{c['divisions'][0].division_id}/workspace"]:
            workspace = client.get(c["base"] + path, headers=headers).json()["data"]
            assert workspace["emergency_notice"] is None
            assert workspace["contest"]["emergency_notice"] is None
            assert workspace["contest"]["emergency_notice_template"] is None

    # Both supported participant token types retain their existing emergency banner.
    for session_kind in ["participant", "general"]:
        headers = participant_headers(c) if session_kind == "participant" else general_headers(c["email"])
        detail = client.get(c["detail"], headers=headers)
        assert detail.json()["data"]["contest"]["emergency_notice"] == expected
        assert detail.json()["data"]["contest"]["emergency_notice_template"] == template
        assert detail.headers["cache-control"] == "private, no-store"
        workspace = client.get(c["base"] + "/workspace", headers=headers).json()["data"]
        assert workspace["emergency_notice"] == expected


def test_public_notice_keeps_emergency_message_visible(contest_context):
    c = contest_context
    store.update_contest_settings(c["cid"], notice_access_after_end=ContestResourceAccess.PUBLIC)
    assert client.get(c["detail"]).json()["data"]["contest"]["emergency_notice"] == "Participant-only emergency notice"


def test_editorial_visibility_does_not_follow_public_problem_visibility(contest_context):
    c = contest_context
    endpoint = c["base"] + f"/problems/{c['problem'].problem_id}"
    store.update_contest_settings(c["cid"], problem_access_after_end=ContestResourceAccess.PUBLIC)
    assert client.get(endpoint).json()["data"]["editorial"] == ""
    assert client.get(endpoint, headers=participant_headers(c)).json()["data"]["editorial"] == "Participant-only editorial"
    store.update_contest_settings(c["cid"], editorial_access_after_end=ContestResourceAccess.PUBLIC)
    assert client.get(endpoint).json()["data"]["editorial"] == "Participant-only editorial"
