"""Exercise the real participant routes with an isolated pre-contest identity."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, ContestResourceAccess, now_utc
from app.orm_models import GeneralSessionRow, ParticipantTeamRow, TeamMemberRow, SubmissionRow
from app.services.store import store
from app.services.storage import object_storage

client = TestClient(app)


def general_login(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def headers(session):
    return {"Authorization": "Bearer " + session["access_token"]}


@pytest.fixture
def preview():
    contest = store.create_contest("Hidden preview", "Test", "Preview testing", start_at=now_utc() + timedelta(days=2), status=ContestStatus.DRAFT)
    cid = contest.contest_id
    store.upsert_contest_operator(cid, f"owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    divisions = [store.create_contest_division(cid, name, name) for name in ("A", "B")]
    problems = [store.create_problem(cid, division.division_id, "A", "Preview problem", "Visible statement", 1000, 128, {}, 1) for division in divisions]
    for problem in problems:
        store.update_problem(cid, problem.problem_id, editorial="PRIVATE EDITORIAL")
    accounts = [store.upsert_contest_operator(cid, f"preview-{uuid4().hex}@zoj.com", "Preview User", ["participant_preview"]) for _ in range(2)]
    sessions = [general_login(str(account.email)) for account in accounts]
    real_email = f"real-{uuid4().hex}@zoj.com"
    real_team = store.create_participant_team(cid, divisions[0].division_id, "Real team", "Real participant", real_email, [])
    real_login = client.post(f"/api/contests/{cid}/participant-login/otp/verify", json={"email": real_email, "otp_code": "", "force_new_session": True})
    assert real_login.status_code == 200
    return {"cid": cid, "divisions": divisions, "problems": problems, "accounts": accounts, "sessions": sessions, "real_team": real_team, "real_session": real_login.json()["data"], "base": f"/api/contests/{cid}", "auth": f"/api/auth/general/contests/{cid}"}


def select_division(c, account=0, division=0):
    response = client.post(c["auth"] + "/participant-preview-session", headers=headers(c["sessions"][account]), json={"division_id": c["divisions"][division].division_id})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def real_running(c):
    store.update_contest_settings(c["cid"], status=ContestStatus.RUNNING, start_at=now_utc() - timedelta(hours=1), end_at=now_utc() + timedelta(hours=2), freeze_at=now_utc() + timedelta(hours=1))


def test_preview_is_an_operator_permission_that_combines_with_other_roles(preview):
    c = preview
    master = general_login("test3@zoj.com")
    endpoint = f"/api/operator/contests/{c['cid']}/operators"
    response = client.post(endpoint, headers=headers(master), json={"email": f"mixed-{uuid4().hex}@zoj.com", "display_name": "Combined operator", "roles": ["participant_preview", "notices_manager"]})
    assert response.status_code == 200, response.text
    combined = general_login(response.json()["data"]["email"])
    assert client.get(c["auth"] + "/participant-preview", headers=headers(combined)).status_code == 200
    assert client.get(endpoint, headers=headers(combined)).status_code == 403
    scopes = c["sessions"][0]["operator_session"]["staff"]["contest_scopes"][c["cid"]]
    assert set(scopes) == {"contest.view", "contest.participant.preview"}
    for path in ["/dashboard", "/divisions"]:
        assert client.get(f"/api/operator/contests/{c['cid']}" + path, headers=headers(c["sessions"][0])).status_code == 200
    for path in ["/problems", "/participants", "/submissions", "/scoreboard/internal", "/notices", "/boards", "/audit-logs"]:
        assert client.get(f"/api/operator/contests/{c['cid']}" + path, headers=headers(c["sessions"][0])).status_code == 403
    assert c["cid"] in {item["contest_id"] for item in client.get("/api/operator/contests", headers=headers(c["sessions"][0])).json()["data"]}
    assert client.get(c["auth"] + "/participant-preview", headers=headers(master)).status_code == 200
    # A permission grant alone does not select a participant identity.
    assert client.get(c["base"] + "/participant-session/me", headers=headers(master)).status_code == 401
    account = store.upsert_contest_operator(c["cid"], f"review-{uuid4().hex}@zoj.com", "Reviewer", ["problem_reviewer"])
    assert client.post(c["auth"] + "/participant-preview-session", headers=headers(general_login(str(account.email))), json={"division_id": c["divisions"][0].division_id}).status_code == 403


def test_combining_roles_keeps_preview_selection_and_operator_access(preview):
    c = preview
    select_division(c)
    account = c["accounts"][0]
    store.update_contest_operator(c["cid"], str(account.email), "Combined", ["participant_preview", "notices_manager"])
    token = headers(c["sessions"][0])
    assert client.get(c["base"] + "/participant-session/me", headers=token).status_code == 200
    assert client.get(f"/api/operator/contests/{c['cid']}/notices", headers=token).status_code == 200
    assert client.get(f"/api/operator/contests/{c['cid']}/participants", headers=token).status_code == 403


def test_legacy_preview_assignment_can_open_operator_home_without_data_migration(preview):
    import json
    from app.orm_models import StaffAccountRow
    c = preview
    with store._session() as db:
        row = db.get(StaffAccountRow, c["accounts"][0].staff_account_id)
        row.contest_scopes = json.dumps({c["cid"]: ["contest.participant.preview"]})
        db.commit()
    token = headers(c["sessions"][0])
    assert client.get(f"/api/operator/contests/{c['cid']}/dashboard", headers=token).status_code == 200
    assert c["cid"] in {item["contest_id"] for item in client.get("/api/operator/contests", headers=token).json()["data"]}
    assert client.get(c["auth"] + "/participant-preview", headers=token).status_code == 200


def test_precontest_real_routes_and_division_switch_without_real_team_creation(preview):
    c = preview
    token = headers(c["sessions"][0])
    with store._session() as db:
        team_count = db.scalar(select(func.count()).select_from(ParticipantTeamRow))
        member_count = db.scalar(select(func.count()).select_from(TeamMemberRow))
    options = client.get(c["auth"] + "/participant-preview", headers=token)
    assert options.status_code == 200
    assert options.json()["data"]["selected_division_id"] is None
    assert client.get(c["base"] + "/workspace", headers=token).status_code == 404
    selection = select_division(c)
    assert selection["is_preview"] is True
    assert selection["team"]["status"] == "preview"
    assert selection["access_token"] == c["sessions"][0]["access_token"]
    assert client.get(c["base"] + "/participant-session/me", headers=token).json()["data"]["is_preview"] is True
    workspace = client.get(c["base"] + "/workspace", headers=token)
    assert workspace.status_code == 200
    assert workspace.json()["data"]["is_preview"] is True
    assert workspace.json()["data"]["division"]["division_id"] == c["divisions"][0].division_id
    assert client.get(c["base"] + "/workspace").status_code == 404
    assert client.get(c["base"] + "/workspace", headers=headers(c["real_session"])).status_code == 404
    assert client.get(f"/api/public/contests/{c['cid']}").status_code == 404
    second_problem = c["base"] + f"/problems/{c['problems'][1].problem_id}"
    assert client.get(second_problem, headers=token).status_code == 404
    select_division(c, division=1)
    assert client.get(second_problem, headers=token).status_code == 200
    assert client.get(c["base"] + f"/problems/{c['problems'][0].problem_id}", headers=token).status_code == 404
    assert client.get(c["auth"] + "/participant-preview", headers=token).json()["data"]["selected_division_id"] == c["divisions"][1].division_id
    with store._session() as db:
        assert db.scalar(select(func.count()).select_from(ParticipantTeamRow)) == team_count
        assert db.scalar(select(func.count()).select_from(TeamMemberRow)) == member_count


def test_preview_assets_hide_editorials_and_judge_files(preview):
    c = preview
    select_division(c)
    cid, pid = c["cid"], c["problems"][0].problem_id
    for category in ["assets", "editorial-assets", "package-files/checker", "support/checker"]:
        key = f"contests/{cid}/problems/{pid}/{category}/image.png"
        object_storage.write_bytes(key, b"private" if category != "assets" else b"visible")
        store.create_problem_asset(cid, pid, "image.png", key, "image/png", 7, "a" * 64)
    token = headers(c["sessions"][0])
    details = client.get(c["base"] + f"/problems/{pid}", headers=token).json()["data"]
    assert details["statement"] == "Visible statement"
    assert details["editorial"] == ""
    assets = client.get(c["base"] + f"/problems/{pid}/assets", headers=token).json()["data"]
    assert len(assets) == 1
    assert client.get(assets[0]["download_url"]).content == b"visible"
    private_key = f"contests/{cid}/problems/{pid}/package-files/checker/image.png"
    assert client.get("/api/storage/objects/" + private_key, headers=token).status_code == 403


def test_preview_submission_runs_real_judge_without_official_submission_or_score(preview):
    c = preview
    select_division(c)
    select_division(c, account=1)
    token = headers(c["sessions"][0])
    before_count = store.count_submissions(contest_id=c["cid"])
    before_board = store.scoreboard_rows(c["cid"], c["divisions"][0].division_id, public_view=True)
    response = client.post(c["base"] + f"/problems/{c['problems'][0].problem_id}/submissions", headers=token, json={"language": "cpp17", "source_code": "int main(){}"})
    assert response.status_code == 200, response.text
    sid = response.json()["data"]["submission_id"]
    saved = store.get_submission(sid)
    assert saved.submission_kind == "participant_preview"
    assert saved.participant_team_id is None and saved.team_member_id is None
    node = store.provision_node(f"preview-test-{uuid4().hex}", "demo", 100, "test")
    jobs = store.claim_jobs(node.judge_node_id, "demo", 100)
    job = next(job for job in jobs if job["submission_id"] == sid)
    result = client.post(f"/api/internal/judge/jobs/{job['judge_job_id']}/result", json={"node_secret": "demo", "lease_token": job["lease_token"], "final_status": "accepted", "judge_message": "SECRET JUDGE DATA"})
    assert result.status_code == 200
    own_list = client.get(c["base"] + "/submissions?include_source=true", headers=token)
    assert own_list.status_code == 200
    assert any(item["submission_id"] == sid for item in own_list.json()["data"])
    own_detail = client.get(c["base"] + f"/submissions/{sid}/status:wait?wait_seconds=0", headers=token).json()["data"]
    assert own_detail["status"] == "accepted" and own_detail["source_code"] == "int main(){}"
    assert own_detail["judge_message"] is None
    assert client.get(c["base"] + f"/submissions/{sid}", headers=headers(c["sessions"][1])).status_code == 404
    assert client.get(c["base"] + "/submissions", headers=headers(c["sessions"][1])).json()["data"] == []
    assert store.count_submissions(contest_id=c["cid"]) == before_count
    assert store.scoreboard_rows(c["cid"], c["divisions"][0].division_id, public_view=True) == before_board
    master = general_login("test3@zoj.com")
    operator_base = f"/api/operator/contests/{c['cid']}/submissions"
    listed = client.get(operator_base, headers=headers(master)).json()["data"]
    assert [item["submission_id"] for item in listed] == [sid]
    assert listed[0]["submitted_by_title"] == "운영진"
    assert listed[0]["source_code"] is None
    detail = client.get(operator_base + "/" + sid, headers=headers(master))
    assert detail.status_code == 200
    assert detail.json()["data"]["source_code"] == "int main(){}"
    assert detail.json()["data"]["judge_message"] == "SECRET JUDGE DATA"
    waited = client.get(operator_base + f"/{sid}/status:wait?wait_seconds=0", headers=headers(master))
    assert waited.status_code == 200
    assert waited.json()["data"]["status"] == "accepted"
    real_running(c)
    assert client.get(c["base"] + "/submissions", headers=headers(c["real_session"])).json()["data"] == []
    assert client.get(c["base"] + f"/submissions/{sid}", headers=headers(c["real_session"])).status_code == 404
    problem = client.get(c["base"] + f"/problems/{c['problems'][0].problem_id}", headers=token).json()["data"]
    assert problem["solve_status"] == "accepted"
    select_division(c, division=1)
    assert client.get(c["base"] + f"/submissions/{sid}", headers=token).status_code == 404


def test_preview_questions_and_replies_are_private_without_real_board_or_mail_writes(preview):
    c = preview
    select_division(c)
    select_division(c, account=1)
    token = headers(c["sessions"][0])
    real = store.get_participant_by_access_token(c["cid"], c["real_session"]["access_token"])
    public = store.create_question(c["cid"], real, "Real public", "Public body", "public")
    private = store.create_question(c["cid"], real, "Real private", "Private body", "private")
    official_count = len(store.contest_questions)
    mail_count = len(store.mail_queue)
    created = client.post(c["base"] + "/boards", headers=token, json={"title": "Preview only", "body": "Isolated body", "visibility": "public"})
    assert created.status_code == 200
    qid = created.json()["data"]["contest_question_id"]
    assert client.post(c["base"] + f"/boards/{qid}/answers", headers=token, json={"body": "Own preview reply"}).status_code == 200
    assert client.post(c["base"] + f"/boards/{public.contest_question_id}/answers", headers=token, json={"body": "Preview reply to real public post"}).status_code == 200
    assert client.post(c["base"] + f"/boards/{private.contest_question_id}/answers", headers=token, json={"body": "Forbidden"}).status_code == 404
    own = client.get(c["base"] + "/boards", headers=token).json()["data"]
    assert {item["contest_question_id"] for item in own} == {qid, public.contest_question_id}
    assert all(len(item["answers"]) == 1 for item in own)
    other = client.get(c["base"] + "/boards", headers=headers(c["sessions"][1])).json()["data"]
    assert len(other) == 1 and other[0]["answers"] == []
    assert len(store.contest_questions) == official_count
    assert len(store.mail_queue) == mail_count
    assert store.get_contest_question(c["cid"], public.contest_question_id).answers == []
    real_running(c)
    actual = client.get(c["base"] + "/boards", headers=headers(c["real_session"])).json()["data"]
    assert {item["contest_question_id"] for item in actual} == {public.contest_question_id, private.contest_question_id}
    assert all(item["answers"] == [] for item in actual)


def test_preview_selection_survives_refresh_but_not_logout_or_permission_removal(preview):
    c = preview
    select_division(c)
    old_headers = headers(c["sessions"][0])
    refreshed = client.post("/api/auth/general/refresh", json={"refresh_token": c["sessions"][0]["refresh_token"]})
    assert refreshed.status_code == 200
    c["sessions"][0] = refreshed.json()["data"]
    current = headers(c["sessions"][0])
    assert client.get(c["base"] + "/participant-session/me", headers=old_headers).status_code == 401
    assert client.get(c["base"] + "/participant-session/me", headers=current).status_code == 200
    store.update_contest_operator(c["cid"], str(c["accounts"][0].email), "Revoked", ["problem_reviewer"])
    assert client.get(c["base"] + "/participant-session/me", headers=current).status_code == 401
    assert client.get(c["base"] + "/workspace", headers=current).status_code == 404
    assert client.post(c["base"] + f"/problems/{c['problems'][0].problem_id}/submissions", headers=current, json={"language": "cpp17", "source_code": "int main(){}"}).status_code == 401
    assert client.post(c["base"] + "/boards", headers=current, json={"title": "Revoked", "body": "Forbidden"}).status_code == 401
    store.update_contest_operator(c["cid"], str(c["accounts"][0].email), "Restored", ["participant_preview"])
    assert client.get(c["base"] + "/participant-session/me", headers=current).status_code == 401
    assert client.get(c["auth"] + "/participant-preview", headers=current).json()["data"]["selected_division_id"] is None
    select_division(c)
    assert client.post("/api/auth/general/logout", headers=current, json={"refresh_token": c["sessions"][0]["refresh_token"]}).status_code == 200
    assert client.get(c["base"] + "/participant-session/me", headers=current).status_code == 401
    c["sessions"][0] = general_login(str(c["accounts"][0].email))
    assert client.get(c["auth"] + "/participant-preview", headers=headers(c["sessions"][0])).json()["data"]["selected_division_id"] is None


def test_preview_cannot_cross_contests_or_divisions_or_create_official_mock_submission(preview):
    c = preview
    select_division(c)
    token = headers(c["sessions"][0])
    other_contest = store.create_contest("Foreign", "Foreign", "Foreign")
    foreign_division = store.create_contest_division(other_contest.contest_id, "F", "Foreign")
    assert client.get(f"/api/auth/general/contests/{other_contest.contest_id}/participant-preview", headers=token).status_code == 403
    assert client.post(c["auth"] + "/participant-preview-session", headers=token, json={"division_id": foreign_division.division_id}).status_code == 404
    assert client.post(c["base"] + f"/problems/{c['problems'][1].problem_id}/submissions", headers=token, json={"language": "cpp17", "source_code": "int main(){}"}).status_code == 404
    assert client.get(c["base"] + f"/divisions/{c['divisions'][1].division_id}/scoreboard", headers=token).status_code == 404
    assert client.get(c["base"] + f"/submissions?division_id={c['divisions'][1].division_id}", headers=token).status_code == 404
    store.update_contest_settings(c["cid"], status=ContestStatus.ENDED, start_at=now_utc() - timedelta(hours=3), end_at=now_utc() - timedelta(hours=1), freeze_at=now_utc() - timedelta(hours=2), mock_judging_enabled=True, problem_access_after_end=ContestResourceAccess.PUBLIC)
    before_count = len(store.submissions)
    assert client.post(c["base"] + f"/problems/{c['problems'][0].problem_id}/mock-submissions", headers=token, json={"language": "cpp17", "source_code": "int main(){}"}).status_code == 403
    assert len(store.submissions) == before_count
    assert client.post(c["base"] + f"/problems/{c['problems'][0].problem_id}/submissions", headers=token, json={"language": "cpp17", "source_code": "int main(){}"}).json()["data"]["submission_kind"] == "participant_preview"


def test_expired_preview_general_session_requires_refresh(preview):
    c = preview
    select_division(c)
    with store._session() as db:
        sessions = db.scalars(select(GeneralSessionRow).where(GeneralSessionRow.email == str(c["accounts"][0].email))).all()
        for session in sessions:
            session.access_expires_at = now_utc() - timedelta(seconds=1)
        db.commit()
    token = headers(c["sessions"][0])
    assert client.get(c["auth"] + "/participant-preview", headers=token).status_code == 401
    assert client.post(c["auth"] + "/participant-preview-session", headers=token, json={"division_id": c["divisions"][0].division_id}).status_code == 401
    assert client.get(c["base"] + "/participant-session/me", headers=token).status_code == 401


def test_operator_contest_list_preserves_legacy_partial_permission_assignments(preview):
    import json
    from app.orm_models import StaffAccountRow

    c = preview
    account = store.upsert_contest_operator(c["cid"], f"legacy-partial-{uuid4().hex}@zoj.com", "Legacy operator", ["notices_manager"])
    with store._session() as db:
        row = db.get(StaffAccountRow, account.staff_account_id)
        row.contest_scopes = json.dumps({c["cid"]: ["contest.notice.create"]})
        row.contest_roles = "{}"
        db.commit()
    response = client.get("/api/operator/contests", headers=headers(general_login(str(account.email))))
    assert response.status_code == 200
    assert [contest["contest_id"] for contest in response.json()["data"]] == [c["cid"]]


def test_operator_log_includes_preview_and_problem_review_with_scoped_access(preview):
    c = preview
    select_division(c)
    before_board = store.scoreboard_rows(c["cid"], c["divisions"][0].division_id, public_view=True)
    source = "int main(){return 0;}"
    created = client.post(c["base"] + f"/problems/{c['problems'][0].problem_id}/submissions", headers=headers(c["sessions"][0]), json={"language": "cpp17", "source_code": source})
    preview_id = created.json()["data"]["submission_id"]
    reviewer = store.upsert_contest_operator(c["cid"], f"reviewer-{uuid4().hex}@zoj.com", "검수 담당", ["problem_reviewer"])
    review_token = headers(general_login(str(reviewer.email)))
    created_review = client.post(f"/api/operator/contests/{c['cid']}/problems/{c['problems'][1].problem_id}/test-submissions", headers=review_token, json={"language": "cpp17", "source_code": source})
    assert created_review.status_code == 200
    review_id = created_review.json()["data"]["submission_id"]
    viewer = store.upsert_contest_operator(c["cid"], f"viewer-{uuid4().hex}@zoj.com", "제출 확인 담당", ["submissions_viewer"])
    viewer_token = headers(general_login(str(viewer.email)))
    base = f"/api/operator/contests/{c['cid']}/submissions"
    listed = client.get(base + "?limit=1", headers=viewer_token)
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["page"]["total_count"] == 2
    assert payload["data"][0]["submission_id"] == review_id
    assert payload["data"][0]["submitted_by_title"] == "검수진"
    second = client.get(base, params={"limit": 1, "cursor": payload["page"]["next_cursor"]}, headers=viewer_token).json()
    assert second["data"][0]["submission_id"] == preview_id
    for sid in [preview_id, review_id]:
        detail = client.get(base + f"/{sid}", headers=viewer_token)
        assert detail.status_code == 200 and detail.json()["data"]["source_code"] == source
        assert client.get(base + f"/{sid}/status:wait?wait_seconds=0", headers=viewer_token).status_code == 200
        assert client.get(base + f"/{sid}", headers=review_token).status_code == 403
        assert client.get(base + f"/{sid}/status:wait?wait_seconds=0", headers=headers(c["sessions"][0])).status_code == 403
    filtered = client.get(base, params={"division_id": c["divisions"][0].division_id, "problem_id": c["problems"][0].problem_id}, headers=viewer_token).json()
    assert [item["submission_id"] for item in filtered["data"]] == [preview_id]
    team_filtered = client.get(base, params={"participant_team_id": c["real_team"].participant_team_id}, headers=viewer_token).json()
    assert team_filtered["data"] == []
    other = store.create_contest("Other", "Test", "Other contest")
    master = headers(general_login("test3@zoj.com"))
    other_base = f"/api/operator/contests/{other.contest_id}/submissions"
    for sid in [preview_id, review_id]:
        assert client.get(other_base + f"/{sid}", headers=master).status_code == 404
        assert client.get(other_base + f"/{sid}/status:wait?wait_seconds=0", headers=master).status_code == 404
    # Opting into the operational log must never make these official records.
    official, _, count = store.list_submissions(contest_id=c["cid"], exclude_operator_tests=True)
    assert official == [] and count == 0
    assert store.scoreboard_rows(c["cid"], c["divisions"][0].division_id, public_view=True) == before_board
