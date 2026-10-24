import os
import base64
import hashlib
import json
import zipfile
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from io import BytesIO
from uuid import uuid4

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, JudgeJobStatus, SubmissionStatus
from app.orm_models import JudgeJobRow, SubmissionRow
from app.services.store import store
from app.services.storage import object_storage


client = TestClient(app)


def patch_with_change_notice(path, **kwargs):
    """Existing settings scenarios explicitly confirm the new notice step.

    Rejection/cancellation and permission boundaries have dedicated tests in
    test_change_notices.py; these scenarios still verify the final policies.
    """
    result = client.patch(path, **kwargs)
    if result.status_code == 409 and result.json()["error"]["code"] == "change_notice_required":
        approval = {**result.json()["error"]["details"], "reason": "운영 시간 조정"}
        result = client.patch(path, **{**kwargs, "json": {**kwargs["json"], "_change_notice": approval}})
    return result


def register_judge_node(*, json):
    json = {"execution_capabilities": ["judge-zoj-runtime-v1"], "execution_revision": "runtime-v1", **json}
    # Provision through the administrator path before testing agent reconnect.
    if not any(n.node_name == json["node_name"] for n in store.judge_nodes.values()):
        store.provision_node(json["node_name"], json["node_secret"], json.get("total_slots", 10))
    return client.post("/api/internal/judge/nodes/register", json=json)


def first_contest_id() -> str:
    public_ids = [contest["contest_id"] for contest in client.get("/api/public/contests").json()["data"]]
    return next(
        contest_id
        for contest_id in public_ids
        if any(team.contest_id == contest_id for team in store.teams.values())
        and any(problem.contest_id == contest_id for problem in store.problems.values())
    )


def set_contest_running(contest_id: str) -> None:
    now = datetime.now(timezone.utc)
    store.update_contest_settings(
        contest_id,
        status=ContestStatus.RUNNING,
        start_at=now - timedelta(hours=1),
        freeze_at=now + timedelta(hours=2),
        end_at=now + timedelta(hours=3),
    )


def set_contest_mutable(contest_id: str) -> None:
    now = datetime.now(timezone.utc)
    store.update_contest_settings(
        contest_id,
        status=ContestStatus.OPEN,
        start_at=now + timedelta(days=1),
        freeze_at=now + timedelta(days=1, hours=3),
        end_at=now + timedelta(days=1, hours=4),
    )


@pytest.fixture(autouse=True)
def reset_demo_contest_window():
    client.cookies.clear()
    set_contest_running(first_contest_id())
    yield


def staff_tokens(email: str = "test3@zoj.com") -> dict:
    response = TestClient(app).post(
        "/api/auth/general/otp/verify",
        json={"email": email, "otp_code": "", "force_new_session": True},
    )
    assert response.status_code == 200
    operator_session = response.json()["data"].get("operator_session")
    assert operator_session
    return operator_session


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def claim_jobs_until(node_id: str, node_secret: str, submission_ids: list[str]) -> dict[str, dict]:
    remaining = set(submission_ids)
    found: dict[str, dict] = {}
    for _ in range(20):
        claim = client.post(
            f"/api/internal/judge/nodes/{node_id}/assignments:claim",
            json={"node_secret": node_secret, "max_count": 100},
        )
        assert claim.status_code == 200
        jobs = claim.json()["data"]["jobs"]
        for job in jobs:
            submission_id = job["submission"]["submission_id"]
            if submission_id in remaining:
                found[submission_id] = job
                remaining.remove(submission_id)
            else:
                client.post(
                    f"/api/internal/judge/jobs/{job['judge_job_id']}/result",
                    json={
                        "node_secret": node_secret,
                        "lease_token": job["lease_token"],
                        "final_status": "system_error",
                    },
                )
        if not remaining:
            return found
        if not jobs:
            break
    missing = ", ".join(sorted(remaining))
    raise AssertionError(f"target judge jobs were not claimed: {missing}")


def jwt_payload(token: str) -> dict:
    parts = token.split(".")
    assert len(parts) == 3
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def participant_login(email: str = "test2@zoj.com") -> tuple[str, dict]:
    contest_id = first_contest_id()
    response = client.post(
        f"/api/contests/{contest_id}/participant-login/otp/verify",
        json={"email": email, "otp_code": "", "force_new_session": True},
    )
    assert response.status_code == 200
    return contest_id, response.json()["data"]


def operator_problem(contest_id: str, division_id: str) -> dict:
    operator = staff_tokens("test4@zoj.com")
    response = client.get(f"/api/operator/contests/{contest_id}/problems", headers=auth_headers(operator["access_token"]))
    assert response.status_code == 200
    return next(item for item in response.json()["data"] if item["division_id"] == division_id)


def test_public_home():
    response = client.get("/api/public/home")
    assert response.status_code == 200
    assert response.json()["data"]["active_contest_count"] >= 1


def test_service_master_general_otp_login_success():
    response = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test3@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert response.status_code == 200
    session = response.json()["data"]
    assert session["access_token"]
    assert session["refresh_token"]
    assert jwt_payload(session["access_token"])["typ"] == "general_access"
    assert jwt_payload(session["refresh_token"])["typ"] == "general_refresh"
    assert session["operator_session"]["default_redirect"] == "/admin"
    assert session["operator_session"]["access_token"]
    assert session["operator_session"]["refresh_token"]
    assert session["operator_session"]["access_token"] == session["access_token"]
    assert session["operator_session"]["refresh_token"] == session["refresh_token"]
    assert jwt_payload(session["operator_session"]["access_token"])["typ"] == "general_access"
    assert jwt_payload(session["operator_session"]["refresh_token"])["typ"] == "general_refresh"


def test_legacy_staff_login_is_removed():
    response = client.post("/api/auth/staff/login", json={"email": "test3@zoj.com", "password": "bad"})
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "staff_login_removed"


def test_operator_general_otp_login_success():
    request = client.post("/api/auth/general/otp/request", json={"email": "test4@zoj.com"})
    assert request.status_code == 200

    code = store.otp_codes["test4@zoj.com"]
    verify = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test4@zoj.com", "otp_code": code, "force_new_session": True},
    )
    assert verify.status_code == 200
    assert verify.json()["data"]["operator_session"]["default_redirect"] == "/operator"
    assert verify.json()["data"]["operator_session"]["access_token"] == verify.json()["data"]["access_token"]
    contests = client.get("/api/operator/contests", headers=auth_headers(verify.json()["data"]["access_token"]))
    assert contests.status_code == 200


def test_general_login_requires_confirmation_when_session_exists():
    first = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test4@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert first.status_code == 200
    first_token = first.json()["data"]["access_token"]

    conflict = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test4@zoj.com", "otp_code": ""},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "session_conflict"
    assert conflict.json()["error"]["details"]["active_session_count"] >= 1

    replaced = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test4@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"]["access_token"] != first_token

    old_session = client.get("/api/auth/general/me", headers=auth_headers(first_token))
    assert old_session.status_code == 401


def test_general_otp_request_is_rate_limited_for_staff_account():
    first = client.post("/api/auth/general/otp/request", json={"email": "test3@zoj.com"})
    assert first.status_code == 200
    assert first.json()["data"]["cooldown_seconds"] == 10

    second = client.post("/api/auth/general/otp/request", json={"email": "test3@zoj.com"})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "otp_request_rate_limited"
    assert second.json()["error"]["details"]["retry_after_seconds"] >= 1


def test_contest_notice_and_board_flow():
    contest_id, participant = participant_login()
    operator = staff_tokens("test4@zoj.com")
    notice_title = f"공지 {uuid4().hex[:8]}"

    notice = client.post(
        f"/api/operator/contests/{contest_id}/notices",
        headers=auth_headers(operator["access_token"]),
        json={
            "title": notice_title,
            "body": "본문 $O(N)$",
            "pinned": True,
            "emergency": True,
            "visibility": "participants",
        },
    )
    assert notice.status_code == 200
    assert notice.json()["data"]["pinned"] is True

    public_notices = client.get(f"/api/contests/{contest_id}/notices")
    assert public_notices.status_code == 200
    assert all(item["visibility"] == "public" for item in public_notices.json()["data"])

    participant_notices = client.get(
        f"/api/contests/{contest_id}/notices",
        headers=auth_headers(participant["access_token"]),
    )
    assert participant_notices.status_code == 200
    assert any(item["title"] == notice_title for item in participant_notices.json()["data"])
    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(staff_tokens()["access_token"]))
    assert mail_queue.status_code == 200
    assert not any(
        item["mail_type"] == "contest_notice_created" and notice_title in item["subject"]
        for item in mail_queue.json()["data"]
    )

    question = client.post(
        f"/api/contests/{contest_id}/boards",
        headers=auth_headers(participant["access_token"]),
        json={"title": "비공개 질문", "body": "질문 내용", "visibility": "private"},
    )
    assert question.status_code == 200
    question_mail = [
        item
        for item in client.get("/api/admin/mail-queue", headers=auth_headers(staff_tokens()["access_token"])).json()["data"]
        if item["mail_type"] == "contest_question_created"
        and "비공개 질문" in item["body_text"]
    ]
    assert question_mail
    assert any("질문 본문" in (item["body_html"] or "") for item in question_mail)
    question_id = question.json()["data"]["contest_question_id"]
    assert question.json()["data"]["author_name"] == participant["member"]["name"]
    assert question.json()["data"]["team_name"] == participant["team"]["team_name"]
    assert question.json()["data"]["division_name"] == participant["division"]["name"]

    public_board = client.get(f"/api/contests/{contest_id}/boards")
    assert public_board.status_code == 200
    assert all(item["visibility"] == "public" for item in public_board.json()["data"])

    hidden = client.patch(
        f"/api/operator/contests/{contest_id}/boards/{question_id}",
        headers=auth_headers(operator["access_token"]),
        json={"visibility": "private"},
    )
    assert hidden.status_code == 200
    assert hidden.json()["data"]["visibility"] == "private"

    public_board = client.get(f"/api/contests/{contest_id}/boards")
    assert public_board.status_code == 200
    assert all(item["contest_question_id"] != question_id for item in public_board.json()["data"])

    visible = client.patch(
        f"/api/operator/contests/{contest_id}/boards/{question_id}",
        headers=auth_headers(operator["access_token"]),
        json={"visibility": "public"},
    )
    assert visible.status_code == 200

    operator_board = client.get(
        f"/api/operator/contests/{contest_id}/boards",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_board.status_code == 200
    assert any(item["contest_question_id"] == question_id for item in operator_board.json()["data"])

    answer = client.post(
        f"/api/operator/contests/{contest_id}/boards/{question_id}/answers",
        headers=auth_headers(operator["access_token"]),
        json={"body": "질문자 전용 답변", "visibility": "questioner"},
    )
    assert answer.status_code == 200
    answer_mail = [
        item
        for item in client.get("/api/admin/mail-queue", headers=auth_headers(staff_tokens()["access_token"])).json()["data"]
        if item["mail_type"] == "contest_question_answered"
        and "질문자 전용 답변" in item["body_text"]
    ]
    assert answer_mail
    assert any("답변 본문" in (item["body_html"] or "") for item in answer_mail)
    assert any("질문 본문" in (item["body_html"] or "") for item in answer_mail)
    assert answer.json()["data"]["created_by_role"] == "operator"
    assert answer.json()["data"]["created_by_name"] == operator["staff"]["display_name"]
    assert answer.json()["data"]["created_by_team_name"] is None
    assert answer.json()["data"]["created_by_division_name"] is None
    participant_answer = client.post(
        f"/api/contests/{contest_id}/boards/{question_id}/answers",
        headers=auth_headers(participant["access_token"]),
        json={"body": "삭제될 참가자 댓글"},
    )
    assert participant_answer.status_code == 200
    participant_answer_id = participant_answer.json()["data"]["contest_answer_id"]
    assert participant_answer.json()["data"]["visibility"] == "public"
    assert participant_answer.json()["data"]["created_by_email"] == participant["member"]["email"]
    assert participant_answer.json()["data"]["created_by_role"] == "participant"
    assert participant_answer.json()["data"]["created_by_name"] == participant["member"]["name"]
    assert participant_answer.json()["data"]["created_by_team_name"] == participant["team"]["team_name"]
    assert participant_answer.json()["data"]["created_by_division_name"] == participant["division"]["name"]
    hidden_answer = client.patch(
        f"/api/operator/contests/{contest_id}/boards/{question_id}/answers/{participant_answer_id}",
        headers=auth_headers(operator["access_token"]),
        json={"visibility": "questioner"},
    )
    assert hidden_answer.status_code == 200
    assert hidden_answer.json()["data"]["visibility"] == "questioner"
    visible_answer = client.patch(
        f"/api/operator/contests/{contest_id}/boards/{question_id}/answers/{participant_answer_id}",
        headers=auth_headers(operator["access_token"]),
        json={"visibility": "public"},
    )
    assert visible_answer.status_code == 200
    deleted_answer = client.delete(
        f"/api/operator/contests/{contest_id}/boards/{question_id}/answers/{participant_answer_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted_answer.status_code == 200
    participant_answer = client.post(
        f"/api/contests/{contest_id}/boards/{question_id}/answers",
        headers=auth_headers(participant["access_token"]),
        json={"body": "참가자 댓글"},
    )
    assert participant_answer.status_code == 200
    assert participant_answer.json()["data"]["created_by_role"] == "participant"

    participant_board = client.get(
        f"/api/contests/{contest_id}/boards",
        headers=auth_headers(participant["access_token"]),
    )
    assert participant_board.status_code == 200
    own_question = next(item for item in participant_board.json()["data"] if item["contest_question_id"] == question_id)
    assert own_question["author_email"] == participant["member"]["email"]
    assert own_question["team_name"] == participant["team"]["team_name"]
    assert own_question["division_name"] == participant["division"]["name"]
    assert own_question["answers"][0]["visibility"] == "questioner"
    assert [item["body"] for item in own_question["answers"]] == ["질문자 전용 답변", "참가자 댓글"]
    assert [item["created_by_role"] for item in own_question["answers"]] == ["operator", "participant"]
    assert own_question["answers"][1]["created_by_team_name"] == participant["team"]["team_name"]
    assert own_question["answers"][1]["created_by_division_name"] == participant["division"]["name"]

    deleted = client.delete(
        f"/api/operator/contests/{contest_id}/boards/{question_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {
        "contest_question_id": question_id,
        "deleted": True,
    }


def test_operator_can_delete_contest_notice():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])

    created = client.post(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
        json={
            "title": "삭제할 공지",
            "body": "삭제 테스트",
            "visibility": "public",
        },
    )
    assert created.status_code == 200
    notice_id = created.json()["data"]["contest_notice_id"]

    deleted = client.delete(
        f"/api/operator/contests/{contest_id}/notices/{notice_id}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {
        "contest_notice_id": notice_id,
        "deleted": True,
    }

    listed = client.get(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
    )
    assert listed.status_code == 200
    assert all(
        item["contest_notice_id"] != notice_id for item in listed.json()["data"]
    )


def test_operator_and_admin_mutations_are_audited():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    operator_headers = auth_headers(operator["access_token"])

    notice = client.post(
        f"/api/operator/contests/{contest_id}/notices",
        headers=operator_headers,
        json={
            "title": "Audit notice",
            "body": "Audit body",
            "visibility": "participants",
        },
    )
    assert notice.status_code == 200

    operator_logs = client.get(
        f"/api/operator/contests/{contest_id}/audit-logs",
        headers=operator_headers,
    )
    assert operator_logs.status_code == 200
    operator_entries = operator_logs.json()["data"]
    assert any(
        entry["method"] == "POST"
        and entry["path"] == f"/api/operator/contests/{contest_id}/notices"
        and entry["contest_id"] == contest_id
        and entry["actor_email"] == operator["staff"]["email"]
        for entry in operator_entries
    )

    master = staff_tokens("test3@zoj.com")
    master_headers = auth_headers(master["access_token"])
    service_notice = client.post(
        "/api/admin/service-notices",
        headers=master_headers,
        json={
            "title": "Audit service notice",
            "summary": "Audit",
            "body": "Audit body",
        },
    )
    assert service_notice.status_code == 200

    admin_logs = client.get(
        "/api/admin/audit-logs?scope=admin",
        headers=master_headers,
    )
    assert admin_logs.status_code == 200
    admin_entries = admin_logs.json()["data"]
    assert any(
        entry["method"] == "POST"
        and entry["path"] == "/api/admin/service-notices"
        and entry["actor_role"] == "service_master"
        and entry["actor_email"] == master["staff"]["email"]
        for entry in admin_entries
    )


def test_access_logs_and_stats_are_visible():
    contest_id, participant = participant_login("test2@zoj.com")
    master = staff_tokens("test3@zoj.com")
    master_headers = auth_headers(master["access_token"])

    admin_logs = client.get(
        f"/api/admin/access-logs?contest_id={contest_id}",
        headers=master_headers,
    )
    assert admin_logs.status_code == 200
    admin_entries = admin_logs.json()["data"]
    assert any(
        entry["event_type"] == "participant_login"
        and entry["contest_id"] == contest_id
        and entry["email"] == participant["member"]["email"]
        and entry["team_name"] == participant["team"]["team_name"]
        for entry in admin_entries
    )

    operator_logs = client.get(
        f"/api/operator/contests/{contest_id}/access-logs",
        headers=master_headers,
    )
    assert operator_logs.status_code == 200
    assert operator_logs.json()["page"]["total_count"] >= 1

    stats = client.get(
        f"/api/operator/contests/{contest_id}/access-log-stats",
        headers=master_headers,
    )
    assert stats.status_code == 200
    assert stats.json()["data"]["success_count"] >= 1


def test_admin_service_notice_management():
    admin = staff_tokens("test3@zoj.com")
    created = client.post(
        "/api/admin/service-notices",
        headers=auth_headers(admin["access_token"]),
        json={
            "title": "서비스 공지",
            "summary": "요약",
            "body": "서비스 공지 본문",
            "emergency": True,
        },
    )
    assert created.status_code == 200
    notice_id = created.json()["data"]["service_notice_id"]

    updated = client.patch(
        f"/api/admin/service-notices/{notice_id}",
        headers=auth_headers(admin["access_token"]),
        json={"summary": "수정 요약", "emergency": False},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["summary"] == "수정 요약"
    assert updated.json()["data"]["emergency"] is False

    public = client.get("/api/public/service-notices")
    assert public.status_code == 200
    assert any(item["service_notice_id"] == notice_id for item in public.json()["data"])

    deleted = client.delete(
        f"/api/admin/service-notices/{notice_id}",
        headers=auth_headers(admin["access_token"]),
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {
        "service_notice_id": notice_id,
        "deleted": True,
    }

    public = client.get("/api/public/service-notices")
    assert public.status_code == 200
    assert all(item["service_notice_id"] != notice_id for item in public.json()["data"])


def test_general_operator_session_me_refresh_and_logout():
    tokens = staff_tokens()

    me = client.get("/api/auth/staff/me", headers=auth_headers(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["data"]["email"] == "test3@zoj.com"

    refreshed = client.post("/api/auth/general/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["access_token"]
    assert refreshed.json()["data"]["refresh_token"] == tokens["refresh_token"]
    assert refreshed.json()["data"]["account"]["email"] == "test3@zoj.com"
    assert refreshed.json()["data"]["operator_session"]["default_redirect"] == "/admin"

    logout = client.post(
        "/api/auth/general/logout",
        headers=auth_headers(refreshed.json()["data"]["access_token"]),
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert logout.status_code == 200
    assert logout.json()["data"]["revoked"] is True


def test_staff_me_requires_access_token():
    response = client.get("/api/auth/staff/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_admin_requires_service_master_token():
    response = client.get("/api/admin/dashboard")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"

    operator = staff_tokens("test4@zoj.com")
    denied = client.get("/api/admin/dashboard", headers=auth_headers(operator["access_token"]))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"

    master = staff_tokens()
    allowed = client.get("/api/admin/dashboard", headers=auth_headers(master["access_token"]))
    assert allowed.status_code == 200


def test_admin_can_bootstrap_contest_divisions_and_operator():
    master = staff_tokens()
    created = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "title": "Production Kickoff",
            "organization_name": "Zerone",
            "overview": "Initial production contest",
            "status": "open",
        },
    )
    assert created.status_code == 200
    contest_id = created.json()["data"]["contest_id"]

    listed_before_assignment = client.get(
        f"/api/operator/contests/{contest_id}/operators",
        headers=auth_headers(master["access_token"]),
    )
    assert listed_before_assignment.status_code == 200
    assert listed_before_assignment.json()["data"] == []

    division = client.post(
        f"/api/admin/contests/{contest_id}/divisions",
        headers=auth_headers(master["access_token"]),
        json={"code": "general", "name": "General", "description": "single division", "display_order": 1},
    )
    assert division.status_code == 200
    assert division.json()["data"]["code"] == "general"

    operator = client.post(
        f"/api/admin/contests/{contest_id}/operators",
        headers=auth_headers(master["access_token"]),
        json={"email": "operator-bootstrap@zoj.com", "display_name": "Bootstrap Operator"},
    )
    assert operator.status_code == 200
    assert operator.json()["data"]["contest_scopes"][contest_id] == ["contest.*", "contest.owner"]


def test_service_master_has_implicit_contest_access_and_is_not_contest_operator():
    contest_id = first_contest_id()
    master = staff_tokens()
    operator = staff_tokens("test4@zoj.com")
    master_email = master["staff"]["email"]

    dashboard = client.get(
        f"/api/operator/contests/{contest_id}/dashboard",
        headers=auth_headers(master["access_token"]),
    )
    assert dashboard.status_code == 200

    listed = client.get(
        f"/api/operator/contests/{contest_id}/operators",
        headers=auth_headers(master["access_token"]),
    )
    assert listed.status_code == 200
    assert all(not item["is_service_master"] for item in listed.json()["data"])
    assert all(item["email"] != master_email for item in listed.json()["data"])

    admin_assign = client.post(
        f"/api/admin/contests/{contest_id}/operators",
        headers=auth_headers(master["access_token"]),
        json={"email": master_email, "display_name": "Should Not Be Listed", "roles": ["master"]},
    )
    assert admin_assign.status_code == 409
    assert admin_assign.json()["error"]["code"] == "service_master_operator_immutable"

    operator_assign = client.post(
        f"/api/operator/contests/{contest_id}/operators",
        headers=auth_headers(operator["access_token"]),
        json={"email": master_email, "display_name": "Should Not Be Listed", "roles": ["master"]},
    )
    assert operator_assign.status_code == 409
    assert operator_assign.json()["error"]["code"] == "service_master_operator_immutable"

    remove_master = client.delete(
        f"/api/operator/contests/{contest_id}/operators/{master_email}",
        headers=auth_headers(operator["access_token"]),
    )
    assert remove_master.status_code == 409
    assert remove_master.json()["error"]["code"] == "service_master_operator_immutable"


def test_service_manager_list_materializes_service_master_contest_scopes():
    master = staff_tokens()
    created = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "title": "Scope Materialized Contest",
            "organization_name": "Zerone",
        },
    )
    assert created.status_code == 200
    contest_id = created.json()["data"]["contest_id"]

    accounts = client.get("/api/admin/service-managers", headers=auth_headers(master["access_token"]))
    assert accounts.status_code == 200
    master_account = next(item for item in accounts.json()["data"] if item["email"] == master["staff"]["email"])
    assert master_account["contest_scopes"][contest_id] == ["contest.*"]


def test_operator_can_create_and_update_division():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    suffix = uuid4().hex[:4]
    division_name = f"Expert {suffix}"

    created = client.post(
        f"/api/operator/contests/{contest_id}/divisions",
        headers=auth_headers(operator["access_token"]),
        json={"code": f"expert{suffix}", "name": division_name, "description": "상위 유형", "display_order": 9},
    )
    assert created.status_code == 200
    division = created.json()["data"]
    assert division["name"] == division_name

    updated = client.patch(
        f"/api/operator/contests/{contest_id}/divisions/{division['division_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"name": f"{division_name} Final", "display_order": 10},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == f"{division_name} Final"
    assert updated.json()["data"]["display_order"] == 10

    duplicate = client.post(
        f"/api/operator/contests/{contest_id}/divisions",
        headers=auth_headers(operator["access_token"]),
        json={"name": f"{division_name} Final", "description": "duplicate name"},
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "validation_error"


def test_operator_mutations_are_locked_during_running_contest():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    participants = client.get(f"/api/operator/contests/{contest_id}/participants", headers=auth_headers(operator["access_token"]))
    team = participants.json()["data"][0]

    division_create = client.post(
        f"/api/operator/contests/{contest_id}/divisions",
        headers=auth_headers(operator["access_token"]),
        json={"name": f"Locked {uuid4().hex[:4]}"},
    )
    assert division_create.status_code == 409
    assert division_create.json()["error"]["code"] == "contest_locked"

    participant_update = client.patch(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"status": "disqualified"},
    )
    assert participant_update.status_code == 409
    assert participant_update.json()["error"]["code"] == "contest_locked"

    problem_create = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"L{uuid4().hex[:6]}",
            "title": "Locked Problem",
            "statement": "should fail",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 99,
        },
    )
    assert problem_create.status_code == 409
    assert problem_create.json()["error"]["code"] == "contest_locked"

    settings_update = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={"status": "ended"},
    )
    assert settings_update.status_code == 409
    assert settings_update.json()["error"]["code"] == "contest_locked"

    emergency_update = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={"emergency_notice": "Queue delay notice"},
    )
    assert emergency_update.status_code == 200

    now = datetime.now(timezone.utc)
    time_update = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "start_at": (now - timedelta(minutes=30)).isoformat(),
            "freeze_at": (now + timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=2)).isoformat(),
        },
    )
    assert time_update.status_code == 200
    emergency_body = time_update.json()["data"]["emergency_notice"]
    assert "시작 시간이" in emergency_body
    assert "프리즈 시간이" in emergency_body
    assert "종료 시간이" in emergency_body
    assert "KST" not in emergency_body
    assert "자동 적용" not in emergency_body
    notices = client.get(
        f"/api/operator/contests/{contest_id}/notices",
        headers=auth_headers(operator["access_token"]),
    )
    assert notices.status_code == 200
    assert any(item["title"] == "대회 일정 변경" and item["emergency"] for item in notices.json()["data"])


def test_auto_time_notice_only_lists_changed_time_fields():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    set_contest_mutable(contest_id)
    contest = store.contests[contest_id]

    update = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={
            "start_at": contest.start_at.replace(second=0, microsecond=0).isoformat(),
            "freeze_at": (contest.freeze_at + timedelta(minutes=10)).isoformat(),
            "end_at": contest.end_at.replace(second=0, microsecond=0).isoformat(),
        },
    )
    assert update.status_code == 200
    emergency_body = update.json()["data"]["emergency_notice"]
    assert "프리즈 시간이" in emergency_body
    assert "시작 시간이" not in emergency_body
    assert "종료 시간이" not in emergency_body
    assert emergency_body.count("변경되었습니다.") == 1


def test_admin_can_create_contest_with_operator_email_only():
    master = staff_tokens()
    created = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "organization_name": "Email Only Org",
            "status": "open",
            "operator_email": "email-only-operator@zoj.com",
        },
    )
    assert created.status_code == 200
    contest = created.json()["data"]
    assert contest["title"] == "Email Only Org Contest"

    accounts = client.get("/api/admin/service-managers", headers=auth_headers(master["access_token"]))
    operator = next(item for item in accounts.json()["data"] if item["email"] == "email-only-operator@zoj.com")
    assert operator["contest_scopes"][contest["contest_id"]] == ["contest.*", "contest.owner"]

    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    queued = [item for item in mail_queue.json()["data"] if item["recipient_email"] == "email-only-operator@zoj.com"]
    assert any(item["mail_type"] == "contest_operator_assigned" for item in queued)


def test_admin_created_contest_defaults_to_draft_and_is_hidden_publicly():
    master = staff_tokens()
    created = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "title": "Draft Contest",
            "organization_name": "Zerone",
            "operator_email": "schedule-tbd-operator@zoj.com",
        },
    )
    assert created.status_code == 200
    contest = created.json()["data"]
    assert contest["status"] == ContestStatus.DRAFT.value

    public_detail = client.get(f"/api/public/contests/{contest['contest_id']}")
    assert public_detail.status_code == 404

    accounts = client.get("/api/admin/service-managers", headers=auth_headers(master["access_token"]))
    master_account = next(item for item in accounts.json()["data"] if item["email"] == "test3@zoj.com")
    assert master_account["contest_scopes"][contest["contest_id"]] == ["contest.*"]


def test_operator_contest_list_includes_scheduled_assigned_contest():
    master = staff_tokens()
    created = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "organization_name": "Hidden Operator Contest",
            "status": "scheduled",
            "operator_email": "scheduled-operator@zoj.com",
        },
    )
    assert created.status_code == 200
    contest = created.json()["data"]

    master_assigned = client.post(
        "/api/admin/contests/{contest_id}/operators".format(contest_id=contest["contest_id"]),
        headers=auth_headers(master["access_token"]),
        json={"email": "test4@zoj.com", "display_name": "Demo Operator"},
    )
    assert master_assigned.status_code == 200

    operator = staff_tokens("test4@zoj.com")
    response = client.get("/api/operator/contests", headers=auth_headers(operator["access_token"]))
    assert response.status_code == 200
    assert any(item["contest_id"] == contest["contest_id"] for item in response.json()["data"])


def test_operator_setting_change_does_not_enqueue_email_notification():
    contest_id = client.get("/api/public/contests").json()["data"][0]["contest_id"]
    operator = staff_tokens("test4@zoj.com")
    master = staff_tokens()
    before_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    before_count = len([item for item in before_queue.json()["data"] if item["mail_type"] == "contest_settings_updated"])

    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={"emergency_notice": "Delayed queue update"},
    )
    assert updated.status_code == 200

    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    after_count = len([item for item in mail_queue.json()["data"] if item["mail_type"] == "contest_settings_updated"])
    assert after_count == before_count


def test_operator_can_update_and_remove_contest_operator():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    email = f"operator-{uuid4().hex[:8]}@zoj.com"
    created = client.post(
        f"/api/operator/contests/{contest_id}/operators",
        headers=auth_headers(operator["access_token"]),
        json={"email": email, "display_name": "Temp Operator", "roles": ["settings_manager"]},
    )
    assert created.status_code == 200

    updated = client.patch(
        f"/api/operator/contests/{contest_id}/operators/{email}",
        headers=auth_headers(operator["access_token"]),
        json={"display_name": "Edited Operator", "roles": ["settings_manager"]},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["display_name"] == "Edited Operator"

    removed = client.delete(
        f"/api/operator/contests/{contest_id}/operators/{email}",
        headers=auth_headers(operator["access_token"]),
    )
    assert removed.status_code == 200

    listed = client.get(f"/api/operator/contests/{contest_id}/operators", headers=auth_headers(operator["access_token"]))
    assert listed.status_code == 200
    assert all(item["email"] != email for item in listed.json()["data"])


def test_operator_requires_contest_scope():
    contest_id = client.get("/api/public/contests").json()["data"][0]["contest_id"]
    response = client.get(f"/api/operator/contests/{contest_id}/dashboard")
    assert response.status_code == 401

    operator = staff_tokens("test4@zoj.com")
    allowed = client.get(f"/api/operator/contests/{contest_id}/dashboard", headers=auth_headers(operator["access_token"]))
    assert allowed.status_code == 200

    master = staff_tokens()
    created = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={"title": "Other Contest", "organization_name": "Zerone", "overview": "scope check"},
    )
    other_contest_id = created.json()["data"]["contest_id"]
    denied = client.get(f"/api/operator/contests/{other_contest_id}/dashboard", headers=auth_headers(operator["access_token"]))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "scope_denied"


def test_operator_problem_asset_and_testcase_metadata_flow():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    target_division_id = next(item for item in divisions.json()["data"] if item["code"] == "beginner")["division_id"]

    problem_code = f"P{uuid4().hex[:6]}"
    created = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": problem_code,
            "title": "MinIO Metadata",
            "statement": "Store testcase object metadata.",
            "time_limit_ms": 2000,
            "memory_limit_mb": 512,
            "display_order": 4,
        },
    )
    assert created.status_code == 200
    problem = created.json()["data"]

    patched = client.patch(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"title": "MinIO Metadata Updated"},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["title"] == "MinIO Metadata Updated"

    moved = client.patch(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"division_id": target_division_id},
    )
    assert moved.status_code == 200
    assert moved.json()["data"]["division_id"] == target_division_id

    asset = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
        headers=auth_headers(operator["access_token"]),
        json={
            "original_filename": "statement.pdf",
            "storage_key": f"contests/{contest_id}/problems/{problem['problem_id']}/statement.pdf",
            "mime_type": "application/pdf",
            "file_size": 1024,
            "sha256": "a" * 64,
        },
    )
    assert asset.status_code == 200
    statement_asset_id = asset.json()["data"]["asset_id"]

    package_asset = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
        headers=auth_headers(operator["access_token"]),
        json={
            "original_filename": "checker.cpp",
            "storage_key": f"contests/{contest_id}/problems/{problem['problem_id']}/package-files/checker/checker.cpp",
            "mime_type": "text/plain",
            "file_size": 256,
            "sha256": "d" * 64,
        },
    )
    assert package_asset.status_code == 200
    package_asset_id = package_asset.json()["data"]["asset_id"]

    testcase_set = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
        json={"is_active": True},
    )
    assert testcase_set.status_code == 200
    testcase_set_id = testcase_set.json()["data"]["testcase_set_id"]

    testcase = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{testcase_set_id}/testcases",
        headers=auth_headers(operator["access_token"]),
        json={
            "display_order": 1,
            "input_storage_key": f"contests/{contest_id}/testcases/input/1.txt",
            "output_storage_key": f"contests/{contest_id}/testcases/output/1.txt",
            "input_sha256": "b" * 64,
            "output_sha256": "c" * 64,
        },
    )
    assert testcase.status_code == 200

    listed = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["testcases"][0]["display_order"] == 1

    second_set = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
        json={"is_active": False},
    )
    assert second_set.status_code == 200

    activated = client.patch(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{second_set.json()['data']['testcase_set_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"is_active": True},
    )
    assert activated.status_code == 200
    assert activated.json()["data"]["is_active"] is True

    set_contest_running(contest_id)
    participant_assets = client.get(
        f"/api/contests/{contest_id}/problems/{problem['problem_id']}/assets",
        headers=auth_headers(participant_login("test1@zoj.com")[1]["access_token"]),
    )
    assert participant_assets.status_code == 200
    visible_assets = participant_assets.json()["data"]
    assert [item["asset_id"] for item in visible_assets] == [statement_asset_id]
    assert visible_assets[0]["download_url"]
    assert all("/package-files/" not in item["storage_key"] for item in visible_assets)

    set_contest_mutable(contest_id)
    deleted = client.delete(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets/{package_asset_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted.status_code == 200

    deleted_problem = client.delete(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted_problem.status_code == 200
    assert deleted_problem.json()["data"]["problem_id"] == problem["problem_id"]

    relisted = client.get(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
    )
    assert relisted.status_code == 200
    assert all(item["problem_id"] != problem["problem_id"] for item in relisted.json()["data"])

    deleted_assets = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted_assets.status_code == 404


def test_operator_presign_upload_returns_storage_key_and_url():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")

    response = client.post(
        f"/api/operator/contests/{contest_id}/storage/presign-upload",
        headers=auth_headers(operator["access_token"]),
        json={"category": "testcases/input", "filename": "sample.txt", "content_type": "text/plain"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["method"] == "PUT"
    assert data["storage_key"].endswith("/testcases/input/sample.txt")
    assert data["upload_url"].startswith("/api/storage/objects/")
    assert "signature=" in data["upload_url"]


def test_minio_browser_urls_use_api_proxy():
    original_backend = object_storage.backend
    object_storage.backend = "minio"
    try:
        assert object_storage.presigned_get_url("contests/demo/problem-assets/a.png").startswith("/api/storage/objects/contests/demo/problem-assets/a.png?expires=")
        assert object_storage.presigned_put_url("contests/demo/testcases/input.txt").startswith("/api/storage/objects/contests/demo/testcases/input.txt?expires=")
    finally:
        object_storage.backend = original_backend


def test_storage_proxy_put_and_get_local_object():
    key = f"tests/proxy/{uuid4().hex}.txt"
    put = client.put(object_storage.presigned_put_url(key), content=b"hello", headers={"content-type": "text/plain"})
    assert put.status_code == 200
    get = client.get(object_storage.presigned_get_url(key))
    assert get.status_code == 200
    assert get.text == "hello"


def test_operator_creates_verified_testcase_set_from_in_out_files():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    suffix = uuid4().hex[:8]
    created = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"V{suffix[:4]}",
            "title": "Verified Testcases",
            "statement": "Validate uploaded in/out files.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 60,
        },
    )
    assert created.status_code == 200
    problem = created.json()["data"]

    def add_asset(role: str, filename: str, source: str) -> dict:
        storage_key = f"contests/{contest_id}/problems/{problem['problem_id']}/package-files/{role}/{suffix}-{filename}"
        object_storage.write_text(storage_key, source)
        response = client.post(
            f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
            headers=auth_headers(operator["access_token"]),
            json={
                "original_filename": filename,
                "storage_key": storage_key,
                "mime_type": "text/plain",
                "file_size": len(source.encode("utf-8")),
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        )
        assert response.status_code == 200

    add_asset("package-resource", "testlib.h", "// testlib placeholder\n")
    add_asset("validator", "validator.py", "import sys\nvalue=sys.stdin.read().strip()\nassert value.isdigit() and int(value) > 0\n")
    add_asset(
        "checker",
        "checker.py",
        "import sys\ninp=int(open(sys.argv[1]).read().strip())\nans=open(sys.argv[2]).read().strip()\nout=open(sys.argv[3]).read().strip()\nassert ans == out\nassert int(ans) == inp * 2\n",
    )
    input_key = f"contests/{contest_id}/problems/{problem['problem_id']}/testcases/{suffix}-001.in"
    output_key = f"contests/{contest_id}/problems/{problem['problem_id']}/testcases/{suffix}-001.out"
    object_storage.write_text(input_key, "7\n")
    object_storage.write_text(output_key, "14\n")

    response = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/verified-testcase-sets",
        headers=auth_headers(operator["access_token"]),
        json={
            "cases": [
                {
                    "display_order": 1,
                    "input_storage_key": input_key,
                    "output_storage_key": output_key,
                    "input_sha256": hashlib.sha256(b"7\n").hexdigest(),
                    "output_sha256": hashlib.sha256(b"14\n").hexdigest(),
                }
            ]
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["verified_count"] == 1
    assert data["testcase_set"]["is_active"] is True
    assert data["testcases"][0]["input_storage_key"] == input_key
    listed = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
    )
    assert listed.status_code == 200
    listed_case = listed.json()["data"][-1]["testcases"][0]
    assert listed_case["input_size_bytes"] == 2
    assert listed_case["output_size_bytes"] == 3

    bad_checker = "import sys\nassert False, 'bad checker must not be accepted'\n"
    bad_checker_key = f"contests/{contest_id}/problems/{problem['problem_id']}/package-files/checker/{suffix}-bad-checker.py"
    object_storage.write_text(bad_checker_key, bad_checker)
    rejected = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
        headers=auth_headers(operator["access_token"]),
        json={
            "original_filename": "checker.py",
            "storage_key": bad_checker_key,
            "mime_type": "text/plain",
            "file_size": len(bad_checker.encode("utf-8")),
            "sha256": hashlib.sha256(bad_checker.encode("utf-8")).hexdigest(),
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "package_asset_verification_failed"


def test_operator_can_delete_testcase_and_testcase_set():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    suffix = uuid4().hex[:8]
    created = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"D{suffix[:4]}",
            "title": "Delete Testcases",
            "statement": "Delete testcase row and set.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 62,
        },
    )
    assert created.status_code == 200
    problem = created.json()["data"]

    set_created = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
        json={"is_active": True},
    )
    assert set_created.status_code == 200
    testcase_set_id = set_created.json()["data"]["testcase_set_id"]

    input_key = f"contests/{contest_id}/problems/{problem['problem_id']}/testcases/{suffix}-delete-001.in"
    output_key = f"contests/{contest_id}/problems/{problem['problem_id']}/testcases/{suffix}-delete-001.out"
    object_storage.write_text(input_key, "1\n")
    object_storage.write_text(output_key, "2\n")
    testcase_created = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{testcase_set_id}/testcases",
        headers=auth_headers(operator["access_token"]),
        json={
            "display_order": 1,
            "input_storage_key": input_key,
            "output_storage_key": output_key,
            "input_sha256": hashlib.sha256(b"1\n").hexdigest(),
            "output_sha256": hashlib.sha256(b"2\n").hexdigest(),
        },
    )
    assert testcase_created.status_code == 200
    testcase_id = testcase_created.json()["data"]["testcase_id"]

    testcase_deleted = client.delete(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{testcase_set_id}/testcases/{testcase_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert testcase_deleted.status_code == 200

    set_deleted = client.delete(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{testcase_set_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert set_deleted.status_code == 200

    listed = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
    )
    assert listed.status_code == 200
    assert all(item["testcase_set_id"] != testcase_set_id for item in listed.json()["data"])


def test_operator_package_status_and_zip_testcase_import():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    suffix = uuid4().hex[:8]
    created = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"Z{suffix[:4]}",
            "title": "Zip Testcase Import",
            "statement": "Import paired zip testcases.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 61,
        },
    )
    assert created.status_code == 200
    problem = created.json()["data"]

    missing = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/package-status",
        headers=auth_headers(operator["access_token"]),
    )
    assert missing.status_code == 200
    assert missing.json()["data"]["ready"] is False
    assert any("validator.cpp" in item for item in missing.json()["data"]["warnings"])
    checker_status = next(
        item for item in missing.json()["data"]["support_files"]
        if item["role"] == "checker"
    )
    assert checker_status["required"] is False
    assert all("checker.cpp" not in item for item in missing.json()["data"]["warnings"])

    def add_asset(role: str, filename: str, source: str):
        storage_key = f"contests/{contest_id}/problems/{problem['problem_id']}/package-files/{role}/{suffix}-{filename}"
        object_storage.write_text(storage_key, source)
        response = client.post(
            f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
            headers=auth_headers(operator["access_token"]),
            json={
                "original_filename": filename,
                "storage_key": storage_key,
                "mime_type": "text/plain",
                "file_size": len(source.encode("utf-8")),
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        )
        assert response.status_code == 200

    add_asset("package-resource", "testlib.h", "// testlib placeholder\n")
    add_asset("validator", "validator.py", "import sys\nvalue=sys.stdin.read().strip()\nassert value.isdigit() and int(value) > 0\n")
    add_asset(
        "checker",
        "checker.py",
        "import sys\ninp=int(open(sys.argv[1]).read().strip())\nans=open(sys.argv[2]).read().strip()\nout=open(sys.argv[3]).read().strip()\nassert ans == out\nassert int(ans) == inp * 2\n",
    )

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("tests/001.in", "7\n")
        zipped.writestr("tests/001.out", "14\n")
        zipped.writestr("tests/sample.in", "8\n")
        zipped.writestr("tests/sample.out", "16\n")

    imported = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/verified-testcase-sets:zip",
        headers=auth_headers(operator["access_token"]),
        files={"file": ("cases.zip", archive.getvalue(), "application/zip")},
    )
    assert imported.status_code == 200
    data = imported.json()["data"]
    assert data["verified_count"] == 2
    assert data["imported_archive"]["case_count"] == 2

    ready = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/package-status",
        headers=auth_headers(operator["access_token"]),
    )
    assert ready.status_code == 200
    assert ready.json()["data"]["ready"] is True
    assert ready.json()["data"]["active_testcase_count"] == 2

    warmed = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/judge-bundle:warm",
        headers=auth_headers(operator["access_token"]),
    )
    assert warmed.status_code == 200
    assert warmed.json()["data"]["testcase_set_id"] == data["testcase_set"]["testcase_set_id"]
    assert warmed.json()["data"]["queue"]["status"] in {"pending", "running", "succeeded"}


def test_package_status_does_not_require_checker_when_testcases_exist():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    suffix = uuid4().hex[:8]
    set_contest_mutable(contest_id)
    created = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": store.contest_divisions(contest_id)[0].division_id,
            "problem_code": f"NOCHK{suffix}",
            "title": "No Checker Status",
            "statement": "Checker is optional for plain testcase judging.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 128,
            "display_order": 62,
        },
    )
    assert created.status_code == 200
    problem = created.json()["data"]

    def add_asset(role: str, filename: str, source: str):
        storage_key = f"contests/{contest_id}/problems/{problem['problem_id']}/package-files/{role}/{suffix}-{filename}"
        object_storage.write_text(storage_key, source)
        response = client.post(
            f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets",
            headers=auth_headers(operator["access_token"]),
            json={
                "original_filename": filename,
                "storage_key": storage_key,
                "mime_type": "text/plain",
                "file_size": len(source.encode("utf-8")),
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        )
        assert response.status_code == 200
        return response.json()["data"]

    add_asset("package-resource", "testlib.h", "// testlib placeholder\n")
    add_asset("validator", "validator.py", "import sys\nsys.stdin.read()\n")
    checker = add_asset(
        "checker",
        "checker.py",
        "import sys\nassert open(sys.argv[2]).read().strip() == open(sys.argv[3]).read().strip()\n",
    )

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("001.in", "1 2\n")
        zipped.writestr("001.out", "3\n")

    imported = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/verified-testcase-sets:zip",
        headers=auth_headers(operator["access_token"]),
        files={"file": ("cases.zip", archive.getvalue(), "application/zip")},
    )
    assert imported.status_code == 200
    deleted_checker = client.delete(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/assets/{checker['asset_id']}",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted_checker.status_code == 200

    status = client.get(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/package-status",
        headers=auth_headers(operator["access_token"]),
    )
    assert status.status_code == 200
    data = status.json()["data"]
    assert data["ready"] is True
    assert data["active_testcase_count"] == 1
    assert all("checker.cpp" not in item for item in data["warnings"])


def test_participant_login_returns_registered_division():
    contest_id = client.get("/api/public/contests").json()["data"][0]["contest_id"]
    otp = client.post(
        f"/api/contests/{contest_id}/participant-login/otp/request",
        json={"email": "test2@zoj.com"},
    )
    assert otp.status_code == 200

    response = client.post(
        f"/api/contests/{contest_id}/participant-login/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["division"]["code"] == "advanced"
    assert data["team"]["division_id"] == data["division"]["division_id"]


def test_participant_login_requires_confirmation_when_session_exists():
    contest_id = client.get("/api/public/contests").json()["data"][0]["contest_id"]
    first = client.post(
        f"/api/contests/{contest_id}/participant-login/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert first.status_code == 200
    first_token = first.json()["data"]["access_token"]

    conflict = client.post(
        f"/api/contests/{contest_id}/participant-login/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": ""},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "session_conflict"

    replaced = client.post(
        f"/api/contests/{contest_id}/participant-login/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"]["access_token"] != first_token

    old_session = client.get(
        f"/api/contests/{contest_id}/participant-session/me",
        headers=auth_headers(first_token),
    )
    assert old_session.status_code == 401


def test_participant_otp_request_is_rate_limited():
    contest_id = client.get("/api/public/contests").json()["data"][0]["contest_id"]
    first = client.post(f"/api/contests/{contest_id}/participant-login/otp/request", json={"email": "test1@zoj.com"})
    assert first.status_code == 200
    assert first.json()["data"]["cooldown_seconds"] == 10

    second = client.post(f"/api/contests/{contest_id}/participant-login/otp/request", json={"email": "test1@zoj.com"})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "otp_request_rate_limited"
    assert second.json()["error"]["details"]["retry_after_seconds"] >= 1


def test_participant_session_me_uses_access_token():
    contest_id, login = participant_login()
    token = login["access_token"]

    me = client.get(f"/api/contests/{contest_id}/participant-session/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["data"]["division"]["code"] == "advanced"

    denied = client.get(f"/api/contests/{contest_id}/participant-session/me")
    assert denied.status_code == 401


def test_general_participant_session_replaces_previous_token():
    contest_id = first_contest_id()
    general = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert general.status_code == 200
    general_token = general.json()["data"]["access_token"]

    first = client.post(
        f"/api/auth/general/contests/{contest_id}/participant-session",
        headers=auth_headers(general_token),
    )
    assert first.status_code == 200
    first_token = first.json()["data"]["access_token"]

    second = client.post(
        f"/api/auth/general/contests/{contest_id}/participant-session",
        headers=auth_headers(general_token),
    )
    assert second.status_code == 200
    second_token = second.json()["data"]["access_token"]
    assert second_token != first_token

    old_session = client.get(
        f"/api/contests/{contest_id}/participant-session/me",
        headers=auth_headers(first_token),
    )
    assert old_session.status_code == 401

    current_session = client.get(
        f"/api/contests/{contest_id}/participant-session/me",
        headers=auth_headers(second_token),
    )
    assert current_session.status_code == 200


def test_operator_revoke_blocks_existing_general_participant_reissue():
    contest_id = first_contest_id()
    general = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert general.status_code == 200
    general_token = general.json()["data"]["access_token"]

    issued = client.post(
        f"/api/auth/general/contests/{contest_id}/participant-session",
        headers=auth_headers(general_token),
    )
    assert issued.status_code == 200
    participant_token = issued.json()["data"]["access_token"]

    operator = staff_tokens("test4@zoj.com")
    participants = client.get(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
    )
    team = next(item for item in participants.json()["data"] if item["team_name"] == issued.json()["data"]["team"]["team_name"])
    member = next(item for item in team["members"] if item["email"] == "test2@zoj.com")
    revoked = client.post(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}/members/{member['team_member_id']}/sessions:revoke",
        headers=auth_headers(operator["access_token"]),
    )
    assert revoked.status_code == 200

    old_session = client.get(
        f"/api/contests/{contest_id}/participant-session/me",
        headers=auth_headers(participant_token),
    )
    assert old_session.status_code == 401

    reissue = client.post(
        f"/api/auth/general/contests/{contest_id}/participant-session",
        headers=auth_headers(general_token),
    )
    assert reissue.status_code == 401


def test_contest_resources_hidden_without_participant_during_contest():
    contest_id = client.get("/api/public/contests").json()["data"][0]["contest_id"]

    problems = client.get(f"/api/contests/{contest_id}/problems")
    assert problems.status_code == 404
    assert problems.json()["error"]["code"] == "not_found"

    scoreboard = client.get(f"/api/contests/{contest_id}/scoreboard")
    assert scoreboard.status_code == 404
    assert scoreboard.json()["error"]["code"] == "not_found"

    submissions = client.get(f"/api/contests/{contest_id}/submissions")
    assert submissions.status_code == 404
    assert submissions.json()["error"]["code"] == "not_found"


def test_public_contest_resources_can_be_viewed_without_participant_during_contest():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])

    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={
            "problem_access_after_end": "public",
            "scoreboard_access_after_end": "public",
            "submission_access_after_end": "public",
            "board_access_after_end": "public",
            "notice_access_after_end": "public",
        },
    )
    assert updated.status_code == 200

    try:
        assert client.get(f"/api/contests/{contest_id}/problems").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/scoreboard").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/submissions").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/boards").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/notices").status_code == 200
    finally:
        restored = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={
                "problem_access_after_end": "private",
                "scoreboard_access_after_end": "private",
                "submission_access_after_end": "private",
                "board_access_after_end": "participants",
                "notice_access_after_end": "public",
                "scoreboard_freeze_mode": "auto",
            },
        )
        assert restored.status_code == 200


def test_public_contest_resources_can_be_viewed_without_participant_after_end():
    contest_id = first_contest_id()
    _contest_id, participant = participant_login()
    assert _contest_id == contest_id
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    now = datetime.now(timezone.utc)

    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={
            "start_at": (now - timedelta(hours=3)).isoformat(),
            "freeze_at": (now - timedelta(hours=2)).isoformat(),
            "end_at": (now - timedelta(hours=1)).isoformat(),
            "problem_access_after_end": "public",
            "scoreboard_access_after_end": "public",
            "submission_access_after_end": "public",
            "board_access_after_end": "public",
            "board_write_after_end": False,
            "notice_access_after_end": "public",
        },
    )
    assert updated.status_code == 200

    try:
        assert client.get(f"/api/contests/{contest_id}/problems").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/scoreboard").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/submissions").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/boards").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/notices").status_code == 200
        blocked_question = client.post(
            f"/api/contests/{contest_id}/boards",
            headers=auth_headers(participant["access_token"]),
            json={"title": "종료 후 질문", "body": "차단", "visibility": "public"},
        )
        assert blocked_question.status_code == 404
        write_enabled = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={"board_write_after_end": True},
        )
        assert write_enabled.status_code == 200
        allowed_question = client.post(
            f"/api/contests/{contest_id}/boards",
            headers=auth_headers(participant["access_token"]),
            json={"title": "종료 후 질문", "body": "허용", "visibility": "public"},
        )
        assert allowed_question.status_code == 200
        allowed_answer = client.post(
            f"/api/contests/{contest_id}/boards/{allowed_question.json()['data']['contest_question_id']}/answers",
            headers=auth_headers(participant["access_token"]),
            json={"body": "종료 후 댓글"},
        )
        assert allowed_answer.status_code == 200
    finally:
        restored = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={
                "problem_access_after_end": "private",
                "scoreboard_access_after_end": "private",
                "submission_access_after_end": "private",
                "board_access_after_end": "participants",
                "board_write_after_end": False,
                "notice_access_after_end": "public",
                "scoreboard_freeze_mode": "auto",
            },
        )
        assert restored.status_code == 200


def test_public_contact_inquiry_can_be_answered_by_service_master():
    master = staff_tokens("test3@zoj.com")
    created = client.post(
        "/api/public/contact-inquiries",
        json={
            "title": "문의 제목",
            "sender_name": "문의자",
            "sender_email": "asker@example.com",
            "body": "문의 본문입니다.",
        },
    )
    assert created.status_code == 200
    inquiry = created.json()["data"]
    assert inquiry["status"] == "pending"
    assert any(
        item["mail_type"] == "contact_inquiry_created"
        and "문의 제목" in item["subject"]
        and "문의 본문" in item["body_html"]
        for item in client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"])).json()["data"]
    )

    listed = client.get(
        "/api/admin/contact-inquiries",
        headers=auth_headers(master["access_token"]),
    )
    assert listed.status_code == 200
    assert any(item["contact_inquiry_id"] == inquiry["contact_inquiry_id"] for item in listed.json()["data"])

    answered = client.post(
        f"/api/admin/contact-inquiries/{inquiry['contact_inquiry_id']}/answer",
        headers=auth_headers(master["access_token"]),
        json={"answer_body": "답변 본문입니다."},
    )
    assert answered.status_code == 200
    assert answered.json()["data"]["status"] == "answered"
    assert "답변 본문입니다." in answered.json()["data"]["answer_body"]
    answered_again = client.post(
        f"/api/admin/contact-inquiries/{inquiry['contact_inquiry_id']}/answer",
        headers=auth_headers(master["access_token"]),
        json={"answer_body": "추가 답변입니다."},
    )
    assert answered_again.status_code == 200
    accumulated_answer = answered_again.json()["data"]["answer_body"]
    assert "답변 본문입니다." in accumulated_answer
    assert "추가 답변입니다." in accumulated_answer
    assert accumulated_answer.count("---") >= 1
    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    assert any(
        item["mail_type"] == "contact_inquiry_answered"
        and item["recipient_email"] == "asker@example.com"
        and "답변 본문입니다." in item["body_text"]
        and "새 답변" in item["body_html"]
        and "문의 본문" in item["body_html"]
        for item in mail_queue.json()["data"]
    )


def test_contest_notice_does_not_enqueue_email():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    problem = next(item for item in store.problems.values() if item.contest_id == contest_id)
    store.create_operator_test_submission(contest_id, problem.problem_id, "cpp17", "int main(){return 0;}")
    title = f"공지 메일 비활성화 {uuid4()}"

    created_notice = client.post(
        f"/api/operator/contests/{contest_id}/notices",
        headers=auth_headers(operator["access_token"]),
        json={"title": title, "body": "공지 이메일 비활성화 확인", "visibility": "participants"},
    )
    assert created_notice.status_code == 200

    matching = [
        item
        for item in store.mail_queue.values()
        if item.mail_type == "contest_notice_created" and item.subject.endswith(title)
    ]
    assert not matching


def test_mock_judging_after_end_is_separate_from_public_submission_list():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    problem = next(item for item in store.problems.values() if item.contest_id == contest_id)
    now = datetime.now(timezone.utc)

    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={
            "start_at": (now - timedelta(hours=3)).isoformat(),
            "freeze_at": (now - timedelta(hours=2)).isoformat(),
            "end_at": (now - timedelta(hours=1)).isoformat(),
            "problem_access_after_end": "public",
            "submission_access_after_end": "public",
            "mock_judging_enabled": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["mock_judging_enabled"] is True

    try:
        created = client.post(
            f"/api/contests/{contest_id}/problems/{problem.problem_id}/mock-submissions",
            json={"language": "cpp17", "source_code": "int main(){return 0;}"},
        )
        assert created.status_code == 200
        created_payload = created.json()["data"]
        submission_id = created_payload["submission_id"]
        assert created_payload["submission_kind"] == "mock_judging"
        assert created_payload["participant_team_id"] is None
        assert created_payload["team_member_id"] is None

        waited = client.get(f"/api/contests/{contest_id}/mock-submissions/{submission_id}/status:wait")
        assert waited.status_code == 200
        assert waited.json()["data"]["source_code"] == ""
        assert waited.json()["data"]["submission_kind"] == "mock_judging"

        public_list = client.get(f"/api/contests/{contest_id}/submissions")
        assert public_list.status_code == 200
        assert all(item["submission_id"] != submission_id for item in public_list.json()["data"])

        operator_list = client.get(
            f"/api/operator/contests/{contest_id}/submissions",
            headers=headers,
        )
        assert operator_list.status_code == 200
        operator_item = next(item for item in operator_list.json()["data"] if item["submission_id"] == submission_id)
        assert operator_item["submission_kind"] == "mock_judging"
        assert operator_item["participant_team_id"] is None

        private_again = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={"problem_access_after_end": "private", "mock_judging_enabled": True},
        )
        assert private_again.status_code == 200
        assert private_again.json()["data"]["mock_judging_enabled"] is False
    finally:
        restored = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={
                "problem_access_after_end": "private",
                "submission_access_after_end": "private",
                "mock_judging_enabled": False,
                "scoreboard_freeze_mode": "auto",
            },
        )
        assert restored.status_code == 200


def test_operator_test_submission_uses_staff_owner_without_participant_team():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    problem = next(item for item in store.problems.values() if item.contest_id == contest_id)
    before_dashboard = client.get(
        f"/api/operator/contests/{contest_id}/dashboard",
        headers=headers,
    )
    assert before_dashboard.status_code == 200
    before_participant_count = before_dashboard.json()["data"]["participant_count"]

    created = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem.problem_id}/test-submissions",
        headers=headers,
        json={"language": "cpp17", "source_code": "int main(){return 0;}"},
    )
    assert created.status_code == 200
    payload = created.json()["data"]
    assert payload["submission_kind"] == "operator_test"
    assert payload["submitted_by_name"] == operator["staff"]["display_name"]
    assert payload["submitted_by_email"] == operator["staff"]["email"]
    assert payload["participant_team_id"] is None
    assert payload["team_member_id"] is None

    after_dashboard = client.get(
        f"/api/operator/contests/{contest_id}/dashboard",
        headers=headers,
    )
    assert after_dashboard.status_code == 200
    assert after_dashboard.json()["data"]["participant_count"] == before_participant_count

    operator_list = client.get(
        f"/api/operator/contests/{contest_id}/submissions",
        headers=headers,
    )
    assert operator_list.status_code == 200
    listed = next(item for item in operator_list.json()["data"] if item["submission_id"] == payload["submission_id"])
    assert listed["submission_kind"] == "operator_test"
    assert listed["submitted_by_name"] == operator["staff"]["display_name"]


def test_public_contest_resources_use_manual_ended_status_even_when_end_time_is_future():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    now = datetime.now(timezone.utc)
    set_contest_mutable(contest_id)

    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={
            "status": "ended",
            "start_at": (now + timedelta(hours=1)).isoformat(),
            "freeze_at": (now + timedelta(hours=3)).isoformat(),
            "end_at": (now + timedelta(hours=4)).isoformat(),
            "problem_access_after_end": "public",
            "scoreboard_access_after_end": "public",
            "submission_access_after_end": "public",
            "board_access_after_end": "public",
            "notice_access_after_end": "public",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["status"] == ContestStatus.ENDED.value

    try:
        public_detail = client.get(f"/api/public/contests/{contest_id}")
        assert public_detail.status_code == 200
        assert public_detail.json()["data"]["contest"]["status"] == ContestStatus.ENDED.value
        assert client.get(f"/api/contests/{contest_id}/problems").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/scoreboard").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/submissions").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/boards").status_code == 200
        assert client.get(f"/api/contests/{contest_id}/notices").status_code == 200
    finally:
        set_contest_mutable(contest_id)
        restored = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={
                "problem_access_after_end": "private",
                "scoreboard_access_after_end": "private",
                "submission_access_after_end": "private",
                "board_access_after_end": "participants",
                "notice_access_after_end": "public",
                "scoreboard_freeze_mode": "auto",
            },
        )
        assert restored.status_code == 200


def test_public_notice_access_after_end_includes_participant_visible_notices():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    now = datetime.now(timezone.utc)
    set_contest_mutable(contest_id)

    created_notice = client.post(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
        json={
            "title": "종료 후 참가자 공지",
            "body": "종료 후 비로그인 공개 대상입니다.",
            "visibility": "participants",
        },
    )
    assert created_notice.status_code == 200

    try:
        updated = patch_with_change_notice(
            f"/api/operator/contests/{contest_id}/settings",
            headers=headers,
            json={
                "start_at": (now - timedelta(hours=3)).isoformat(),
                "freeze_at": (now - timedelta(hours=2)).isoformat(),
                "end_at": (now - timedelta(hours=1)).isoformat(),
                "notice_access_after_end": "public",
            },
        )
        assert updated.status_code == 200

        public_notices = client.get(f"/api/contests/{contest_id}/notices")
        assert public_notices.status_code == 200
        assert any(
            notice["title"] == "종료 후 참가자 공지"
            for notice in public_notices.json()["data"]
        )
    finally:
        set_contest_mutable(contest_id)
        restored_notice = client.patch(
            f"/api/operator/contests/{contest_id}/notices/{created_notice.json()['data']['contest_notice_id']}",
            headers=headers,
            json={"visibility": "public"},
        )
        assert restored_notice.status_code == 200


def test_general_participant_can_view_participant_notice_before_start():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    set_contest_mutable(contest_id)

    created_notice = client.post(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
        json={
            "title": "진행 전 참가자 공지",
            "body": "일반 로그인 참가자에게 보여야 합니다.",
            "visibility": "participants",
        },
    )
    assert created_notice.status_code == 200

    general = client.post(
        "/api/auth/general/otp/verify",
        json={"email": "test2@zoj.com", "otp_code": "", "force_new_session": True},
    )
    assert general.status_code == 200

    try:
        public_notices = client.get(f"/api/contests/{contest_id}/notices")
        assert public_notices.status_code == 200
        assert all(
            notice["title"] != "진행 전 참가자 공지"
            for notice in public_notices.json()["data"]
        )

        participant_notices = client.get(
            f"/api/contests/{contest_id}/notices",
            headers=auth_headers(general.json()["data"]["access_token"]),
        )
        assert participant_notices.status_code == 200
        assert any(
            notice["title"] == "진행 전 참가자 공지"
            for notice in participant_notices.json()["data"]
        )
    finally:
        client.delete(
            f"/api/operator/contests/{contest_id}/notices/{created_notice.json()['data']['contest_notice_id']}",
            headers=headers,
        )


def test_operator_can_manually_override_public_scoreboard_freeze_mode():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])

    frozen = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={"scoreboard_freeze_mode": "frozen"},
    )
    assert frozen.status_code == 200
    assert frozen.json()["data"]["scoreboard_freeze_mode"] == "frozen"
    public_state = client.get(
        f"/api/operator/contests/{contest_id}/scoreboard/internal",
        headers=headers,
    )
    assert public_state.status_code == 200
    assert public_state.json()["data"]["frozen_public_view"] is True

    live = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={"scoreboard_freeze_mode": "live"},
    )
    assert live.status_code == 200
    assert live.json()["data"]["scoreboard_freeze_mode"] == "live"
    public_state = client.get(
        f"/api/operator/contests/{contest_id}/scoreboard/internal",
        headers=headers,
    )
    assert public_state.status_code == 200
    assert public_state.json()["data"]["frozen_public_view"] is False

    restored = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={"scoreboard_freeze_mode": "auto"},
    )
    assert restored.status_code == 200


def test_operator_presentation_scoreboard_uses_public_freeze_view():
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])

    frozen = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={"scoreboard_freeze_mode": "frozen"},
    )
    assert frozen.status_code == 200

    presentation = client.get(
        f"/api/operator/contests/{contest_id}/scoreboard/presentation",
        headers=headers,
    )
    assert presentation.status_code == 200
    data = presentation.json()["data"]
    assert data["contest"]["contest_id"] == contest_id
    assert data["sections"]
    assert all("division" in section for section in data["sections"])
    assert all("problems" in section for section in data["sections"])
    assert all(section["frozen"] is True for section in data["sections"])

    restored = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=headers,
        json={"scoreboard_freeze_mode": "auto"},
    )
    assert restored.status_code == 200


def test_participant_cannot_view_problems_before_contest_start():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)

    problems = client.get(f"/api/contests/{contest_id}/problems", headers=auth_headers(login["access_token"]))
    assert problems.status_code == 404
    assert problems.json()["error"]["code"] == "not_found"

def test_operator_updates_contest_settings_and_public_after_end_policy():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    now = datetime.now(timezone.utc)
    start_at = now - timedelta(hours=3)
    freeze_at = now - timedelta(hours=2)
    end_at = now - timedelta(hours=1)
    problem_id = next(
        problem.problem_id
        for problem in store.problems.values()
        if problem.contest_id == contest_id
    )
    editorial = "# 해설\n\n대회 종료 후 공개되는 해설입니다."

    patched_problem = client.patch(
        f"/api/operator/contests/{contest_id}/problems/{problem_id}",
        headers=auth_headers(operator["access_token"]),
        json={"editorial": editorial},
    )
    assert patched_problem.status_code == 200
    assert patched_problem.json()["data"]["editorial"] == editorial

    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "status": "ended",
            "start_at": start_at.isoformat(),
            "freeze_at": freeze_at.isoformat(),
            "end_at": end_at.isoformat(),
            "problem_access_after_end": "public",
            "editorial_access_after_end": "public",
            "scoreboard_access_after_end": "public",
            "submission_access_after_end": "public",
            "emergency_notice": "Final review in progress",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["status"] == "ended"
    assert updated.json()["data"]["problem_access_after_end"] == "public"

    public_problems = client.get(f"/api/contests/{contest_id}/problems")
    assert public_problems.status_code == 200
    public_problem = client.get(f"/api/contests/{contest_id}/problems/{problem_id}")
    assert public_problem.status_code == 200
    assert public_problem.json()["data"]["editorial"] == editorial

    private_editorial = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={"editorial_access_after_end": "private"},
    )
    assert private_editorial.status_code == 200
    hidden_problem = client.get(f"/api/contests/{contest_id}/problems/{problem_id}")
    assert hidden_problem.status_code == 200
    assert hidden_problem.json()["data"]["editorial"] == ""

    invalid = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={"start_at": end_at.isoformat(), "end_at": start_at.isoformat()},
    )
    assert invalid.status_code == 422

    restore_start = now - timedelta(hours=1)
    restore_end = now + timedelta(hours=3)
    restore_freeze = now + timedelta(hours=2)
    restored = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "status": "running",
            "start_at": restore_start.isoformat(),
            "freeze_at": restore_freeze.isoformat(),
            "end_at": restore_end.isoformat(),
            "problem_access_after_end": "private",
            "editorial_access_after_end": "private",
            "scoreboard_access_after_end": "private",
            "submission_access_after_end": "private",
            "emergency_notice": "제출 지연은 long polling 상태창에서 확인하세요.",
        },
    )
    assert restored.status_code == 200


def test_contest_status_is_normalized_from_schedule_updates():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    now = datetime.now(timezone.utc)
    future_start = now + timedelta(days=2)
    future_freeze = future_start + timedelta(hours=3)
    future_end = future_start + timedelta(hours=4)

    future = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "status": "running",
            "start_at": future_start.isoformat(),
            "freeze_at": future_freeze.isoformat(),
            "end_at": future_end.isoformat(),
        },
    )
    assert future.status_code == 200
    assert future.json()["data"]["status"] == "open"

    running_start = now - timedelta(minutes=10)
    running_freeze = now + timedelta(hours=1)
    running_end = now + timedelta(hours=2)
    running = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "status": "open",
            "start_at": running_start.isoformat(),
            "freeze_at": running_freeze.isoformat(),
            "end_at": running_end.isoformat(),
        },
    )
    assert running.status_code == 200
    assert running.json()["data"]["status"] == "running"


def test_operator_schedule_update_auto_adjusts_end_and_freeze_when_only_start_is_changed():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    baseline = client.get(f"/api/operator/contests/{contest_id}/dashboard", headers=auth_headers(operator["access_token"]))
    assert baseline.status_code == 200
    before = baseline.json()["data"]["contest"]
    old_start = datetime.fromisoformat(before["start_at"].replace("Z", "+00:00"))
    old_end = datetime.fromisoformat(before["end_at"].replace("Z", "+00:00"))
    duration = old_end - old_start

    moved_start = old_end + timedelta(hours=2)
    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "start_at": moved_start.isoformat(),
        },
    )
    assert updated.status_code == 200
    data = updated.json()["data"]
    assert datetime.fromisoformat(data["start_at"].replace("Z", "+00:00")) == moved_start
    expected_end = moved_start + duration
    assert datetime.fromisoformat(data["end_at"].replace("Z", "+00:00")) == expected_end
    freeze = datetime.fromisoformat(data["freeze_at"].replace("Z", "+00:00"))
    assert moved_start <= freeze <= expected_end

def test_operator_updates_participant_team_status_and_division():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    participants = client.get(f"/api/operator/contests/{contest_id}/participants", headers=auth_headers(operator["access_token"]))
    team = next(item for item in participants.json()["data"] if item["team_name"] == "Team Rookie")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    advanced = next(item for item in divisions.json()["data"] if item["code"] == "advanced")
    beginner = next(item for item in divisions.json()["data"] if item["code"] == "beginner")

    updated = client.patch(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"team_name": "Team Rookie Updated", "division_id": advanced["division_id"], "status": "disqualified"},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["team_name"] == "Team Rookie Updated"
    assert updated.json()["data"]["division"]["code"] == "advanced"
    assert updated.json()["data"]["status"] == "disqualified"

    invalid = client.patch(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"status": "deleted"},
    )
    assert invalid.status_code == 422

    restored = client.patch(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"team_name": "Team Rookie", "division_id": beginner["division_id"], "status": "active"},
    )
    assert restored.status_code == 200


def test_operator_bulk_creates_participant_teams():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    suffix = uuid4().hex[:8]

    bulk = client.post(
        f"/api/operator/contests/{contest_id}/participants:bulk-create",
        headers=auth_headers(operator["access_token"]),
        json={
            "teams": [
                {
                    "team_name": f"Bulk Team {suffix}",
                    "division_id": division_id,
                    "leader": {"name": "Bulk Leader", "email": f"bulk-leader-{suffix}@zoj.com"},
                    "members": [{"name": "Bulk Member", "email": f"bulk-member-{suffix}@zoj.com"}],
                }
            ]
        },
    )
    assert bulk.status_code == 200
    assert len(bulk.json()["data"]["created"]) == 1
    assert bulk.json()["data"]["errors"] == []

    duplicate = client.post(
        f"/api/operator/contests/{contest_id}/participants:bulk-create",
        headers=auth_headers(operator["access_token"]),
        json={
            "teams": [
                {
                    "team_name": f"Bulk Team Duplicate {suffix}",
                    "division_id": division_id,
                    "leader": {"name": "Bulk Leader", "email": f"bulk-leader-{suffix}@zoj.com"},
                    "members": [],
                }
            ]
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["created"] == []
    assert duplicate.json()["data"]["errors"][0]["message"] == "participant email already registered"


def test_participant_invite_mail_is_html_and_sent_when_public():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    master = staff_tokens()
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division = divisions.json()["data"][0]
    suffix = uuid4().hex[:8]
    leader_email = f"invite-leader-{suffix}@zoj.com"

    created = client.post(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division["division_id"],
            "team_name": f"Invite Team {suffix}",
            "leader": {"name": "Invite Leader", "email": leader_email},
            "members": [],
        },
    )
    assert created.status_code == 200

    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    invite = next(item for item in mail_queue.json()["data"] if item["recipient_email"] == leader_email)
    assert invite["mail_type"] == "participant_invited"
    assert "대회에 초대되었습니다" in invite["subject"]
    assert "body_html" in invite and "대회 페이지 열기" in invite["body_html"]
    assert f"/contests/{contest_id}" in invite["body_html"]


def test_private_participant_invites_are_sent_when_contest_becomes_public():
    master = staff_tokens()
    created_contest = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "organization_name": "Delayed Invite Org",
            "status": "scheduled",
            "start_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "freeze_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
            "operator_email": "delayed-invite-operator@zoj.com",
        },
    )
    assert created_contest.status_code == 200
    contest_id = created_contest.json()["data"]["contest_id"]
    division = client.post(
        f"/api/admin/contests/{contest_id}/divisions",
        headers=auth_headers(master["access_token"]),
        json={"code": "main", "name": "Main", "description": ""},
    )
    assert division.status_code == 200
    operator = staff_tokens("delayed-invite-operator@zoj.com")
    suffix = uuid4().hex[:8]
    leader_email = f"delayed-invite-{suffix}@zoj.com"

    created_team = client.post(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division.json()["data"]["division_id"],
            "team_name": f"Delayed Invite {suffix}",
            "leader": {"name": "Delayed Leader", "email": leader_email},
            "members": [],
        },
    )
    assert created_team.status_code == 200
    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    assert not any(item["mail_type"] == "participant_invited" and item["recipient_email"] == leader_email for item in mail_queue.json()["data"])

    now = datetime.now(timezone.utc)
    opened = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "status": "open",
            "start_at": (now + timedelta(hours=2)).isoformat(),
            "freeze_at": (now + timedelta(hours=5)).isoformat(),
            "end_at": (now + timedelta(hours=6)).isoformat(),
        },
    )
    assert opened.status_code == 200

    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    assert any(item["mail_type"] == "participant_invited" and item["recipient_email"] == leader_email for item in mail_queue.json()["data"])


def test_due_contest_reminders_enqueue_html_mail_once():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    master = staff_tokens()
    now = datetime.now(timezone.utc)
    updated = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "status": "open",
            "start_at": (now + timedelta(minutes=9)).isoformat(),
            "freeze_at": (now + timedelta(hours=2)).isoformat(),
            "end_at": (now + timedelta(hours=3)).isoformat(),
        },
    )
    assert updated.status_code == 200

    first_count = store.enqueue_due_contest_reminders()
    second_count = store.enqueue_due_contest_reminders()
    assert first_count > 0
    assert second_count == 0

    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(master["access_token"]))
    reminder = next(item for item in mail_queue.json()["data"] if item["mail_type"] == "contest_reminder_10m")
    assert "시작 10분 전 안내" in reminder["subject"]
    assert "body_html" in reminder and "대회 페이지 열기" in reminder["body_html"]


def test_due_contest_emergency_notices_for_freeze_and_end_are_once(monkeypatch):
    contest_id = first_contest_id()
    operator = staff_tokens("test4@zoj.com")
    headers = auth_headers(operator["access_token"])
    scheduled_titles = {
        f"{target} {label} 전"
        for target in ("스코어보드 프리즈", "대회 종료")
        for label in ("30분", "10분", "5분", "1분")
    }
    scheduled_titles.update({"스코어보드 프리즈 시작", "대회 종료", "스코어보드 공개됨", "스코어보드 프리즈 안내", "대회 종료 안내"})
    for notice in store.contest_notices_for_view(contest_id, operator=True):
        if notice.title in scheduled_titles:
            store.delete_contest_notice(contest_id, notice.contest_notice_id)
    now = datetime.now(timezone.utc)
    from importlib import import_module
    monkeypatch.setattr(import_module("app.services.store"), "now_utc", lambda: now)

    store.update_contest_settings(
        contest_id,
        status=ContestStatus.RUNNING,
        start_at=now - timedelta(hours=1),
        freeze_at=now + timedelta(minutes=9),
        end_at=now + timedelta(hours=1),
        emergency_notice="",
    )
    freeze_count = store.enqueue_due_contest_emergency_notices()
    duplicate_freeze_count = store.enqueue_due_contest_emergency_notices()
    assert freeze_count >= 1
    assert duplicate_freeze_count == 0

    freeze_notice = client.get(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
    )
    assert freeze_notice.status_code == 200
    freeze_notices = freeze_notice.json()["data"]
    assert any(
        item["title"] == "스코어보드 프리즈 안내"
        and item["emergency"]
        and item["pinned"]
        and "스코어보드 프리즈까지 9분 남았습니다" in item["body"]
        for item in freeze_notices
    )
    assert "스코어보드 프리즈까지 9분 남았습니다" in store.contests[contest_id].emergency_notice

    store.update_contest_settings(
        contest_id,
        status=ContestStatus.RUNNING,
        start_at=now - timedelta(hours=1),
        freeze_at=now - timedelta(minutes=1),
        end_at=now + timedelta(minutes=4),
        emergency_notice="",
    )
    end_count = store.enqueue_due_contest_emergency_notices()
    duplicate_end_count = store.enqueue_due_contest_emergency_notices()
    assert end_count >= 1
    assert duplicate_end_count == 0

    end_notice = client.get(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
    )
    assert end_notice.status_code == 200
    end_notices = end_notice.json()["data"]
    assert any(
        item["title"] == "대회 종료 안내"
        and item["emergency"]
        and item["pinned"]
        and "대회 종료까지 4분 남았습니다" in item["body"]
        for item in end_notices
    )
    assert "대회 종료까지 4분 남았습니다" in store.contests[contest_id].emergency_notice

    store.update_contest_settings(
        contest_id,
        status=ContestStatus.RUNNING,
        start_at=now - timedelta(hours=2),
        freeze_at=now - timedelta(hours=1),
        end_at=now - timedelta(minutes=1),
        scoreboard_access_after_end="public",
        emergency_notice="",
    )
    ended_count = store.enqueue_due_contest_emergency_notices()
    duplicate_ended_count = store.enqueue_due_contest_emergency_notices()
    assert ended_count >= 1
    assert duplicate_ended_count == 0

    ended_notice = client.get(
        f"/api/operator/contests/{contest_id}/notices",
        headers=headers,
    )
    assert ended_notice.status_code == 200
    ended_notices = ended_notice.json()["data"]
    assert any(
        item["title"] == "대회 종료 안내"
        and item["emergency"]
        and item["pinned"]
        and "대회가 종료되었습니다" in item["body"]
        for item in ended_notices
    )
    assert not any(
        item["title"] == "스코어보드 공개됨"
        and item["emergency"]
        and item["pinned"]
        and "스코어보드가 공개되었습니다" in item["body"]
        for item in ended_notices
    )


def test_participant_email_conflict_with_staff_is_scoped_to_same_contest():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    suffix = uuid4().hex[:8]

    master = staff_tokens()
    other_operator_email = f"other-operator-{suffix}@zoj.com"
    other_contest = client.post(
        "/api/admin/contests",
        headers=auth_headers(master["access_token"]),
        json={
            "organization_name": f"Other Org {suffix}",
            "operator_email": other_operator_email,
        },
    )
    assert other_contest.status_code == 200

    allowed = client.post(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
        json={
            "team_name": f"Scoped Staff Team {suffix}",
            "division_id": division_id,
            "leader": {"name": "Scoped Leader", "email": other_operator_email},
            "members": [],
        },
    )
    assert allowed.status_code == 200

    denied = client.post(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
        json={
            "team_name": f"Same Contest Staff Team {suffix}",
            "division_id": division_id,
            "leader": {"name": "Same Contest Staff", "email": "test4@zoj.com"},
            "members": [],
        },
    )
    assert denied.status_code == 422
    assert "participant email cannot be operator/staff account" in denied.json()["error"]["message"]


def test_operator_deletes_participant_team_without_history():
    contest_id = first_contest_id()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    divisions = client.get(f"/api/operator/contests/{contest_id}/divisions", headers=auth_headers(operator["access_token"]))
    division_id = next(item for item in divisions.json()["data"] if item["code"] == "advanced")["division_id"]
    suffix = uuid4().hex[:8]
    leader_email = f"delete-leader-{suffix}@zoj.com"

    created = client.post(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
        json={
            "team_name": f"Delete Team {suffix}",
            "division_id": division_id,
            "leader": {"name": "Delete Leader", "email": leader_email},
            "members": [],
        },
    )
    assert created.status_code == 200
    team_id = created.json()["data"]["participant_team_id"]

    deleted = client.delete(
        f"/api/operator/contests/{contest_id}/participants/{team_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    participants = client.get(f"/api/operator/contests/{contest_id}/participants", headers=auth_headers(operator["access_token"]))
    assert all(item["participant_team_id"] != team_id for item in participants.json()["data"])
    mail_queue = client.get("/api/admin/mail-queue", headers=auth_headers(staff_tokens()["access_token"]))
    deleted_team_mail = [
        item
        for item in mail_queue.json()["data"]
        if item["recipient_email"] == leader_email
        and item["mail_type"] in {"participant_invited", "contest_reminder_24h", "contest_reminder_1h", "contest_reminder_10m"}
    ]
    assert deleted_team_mail
    assert all(item["status"] == "canceled" for item in deleted_team_mail)


def test_operator_manages_team_members_and_revokes_member_sessions():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    participants = client.get(f"/api/operator/contests/{contest_id}/participants", headers=auth_headers(operator["access_token"]))
    team = next(item for item in participants.json()["data"] if item["team_name"] == login["team"]["team_name"])
    suffix = uuid4().hex[:8]
    member_email = f"new-member-{suffix}@zoj.com"
    renamed_email = f"renamed-member-{suffix}@zoj.com"

    created = client.post(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}/members",
        headers=auth_headers(operator["access_token"]),
        json={"name": "New Member", "email": member_email},
    )
    assert created.status_code == 200
    member = created.json()["data"]
    assert member["role"] == "member"

    duplicate = client.post(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}/members",
        headers=auth_headers(operator["access_token"]),
        json={"name": "Duplicate", "email": "test2@zoj.com"},
    )
    assert duplicate.status_code == 422

    updated = client.patch(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}/members/{member['team_member_id']}",
        headers=auth_headers(operator["access_token"]),
        json={"name": "Renamed Member", "email": renamed_email},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "Renamed Member"
    assert updated.json()["data"]["email"] == renamed_email

    leader = next(item for item in team["members"] if item["role"] == "leader")
    revoked = client.post(
        f"/api/operator/contests/{contest_id}/participants/{team['participant_team_id']}/members/{leader['team_member_id']}/sessions:revoke",
        headers=auth_headers(operator["access_token"]),
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["active_sessions"] == 0

    denied = client.get(f"/api/contests/{contest_id}/participant-session/me", headers=auth_headers(login["access_token"]))
    assert denied.status_code == 401


def test_submission_rejects_problem_from_other_division():
    contest_id, login = participant_login()
    contest_detail = client.get(f"/api/public/contests/{contest_id}").json()["data"]
    beginner_division = next(item for item in contest_detail["divisions"] if item["code"] == "beginner")
    beginner_problem = operator_problem(contest_id, beginner_division["division_id"])

    response = client.post(
        f"/api/contests/{contest_id}/problems/{beginner_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "cpp17", "source_code": "int main(){return 0;}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_judge_claim_uses_database_queue():
    contest_id, login = participant_login()
    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]

    submission = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "cpp17", "source_code": "int main(){return 0;}"},
    )
    assert submission.status_code == 200
    submission_id = submission.json()["data"]["submission_id"]

    node = register_judge_node(
        json={"node_name": "pytest-node", "node_secret": "demo", "total_slots": 10},
    )
    assert node.status_code == 200
    node_id = node.json()["data"]["judge_node_id"]

    claimed = claim_jobs_until(node_id, "demo", [submission_id])[submission_id]
    assert claimed["submission"]["division_id"] == advanced_division["division_id"]


def test_judge_node_secret_is_required_for_claim_and_result():
    contest_id, login = participant_login()
    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]
    client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(42)"},
    )

    node = register_judge_node(
        json={"node_name": f"secret-node-{uuid4().hex[:6]}", "node_secret": "correct", "total_slots": 1},
    )
    node_id = node.json()["data"]["judge_node_id"]

    denied = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": "wrong", "max_count": 1},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "node_secret_invalid"

    claim = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": "correct", "max_count": 1},
    )
    assert claim.status_code == 200
    job = claim.json()["data"]["jobs"][0]

    result_denied = client.post(
        f"/api/internal/judge/jobs/{job['judge_job_id']}/result",
        json={
            "node_secret": "wrong",
            "lease_token": job["lease_token"],
            "final_status": "accepted",
        },
    )
    assert result_denied.status_code == 403
    assert result_denied.json()["error"]["code"] == "node_secret_invalid"


def test_service_master_can_view_judge_agent_logs():
    master = staff_tokens("test3@zoj.com")
    node_secret = f"log-secret-{uuid4().hex[:6]}"
    node = register_judge_node(
        json={"node_name": f"log-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 1},
    )
    assert node.status_code == 200
    node_id = node.json()["data"]["judge_node_id"]

    denied = client.post(
        f"/api/internal/judge/nodes/{node_id}/logs",
        json={"node_secret": "wrong", "logs": [{"level": "info", "message": "should not be stored"}]},
    )
    assert denied.status_code == 403

    appended = client.post(
        f"/api/internal/judge/nodes/{node_id}/logs",
        json={
            "node_secret": node_secret,
            "logs": [
                {"level": "info", "message": "[judge-agent] claimed job"},
                {"level": "error", "message": "[judge-agent] heartbeat failed"},
            ],
        },
    )
    assert appended.status_code == 200
    assert appended.json()["data"]["accepted"] == 2

    logs = client.get(
        f"/api/admin/judge/nodes/{node_id}/logs",
        headers=auth_headers(master["access_token"]),
    )
    assert logs.status_code == 200
    messages = [item["message"] for item in logs.json()["data"]]
    assert "[judge-agent] heartbeat failed" in messages
    assert "[judge-agent] claimed job" in messages
    assert logs.json()["page"]["total_count"] >= 2


def test_submission_progress_is_updated_during_judging():
    contest_id, login = participant_login()
    store.update_contest_settings(contest_id, participant_progress_visible=True)
    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]
    submission = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(42)"},
    )
    assert submission.status_code == 200
    submission_id = submission.json()["data"]["submission_id"]

    node_secret = "progress-secret"
    node = register_judge_node(
        json={"node_name": f"progress-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 1},
    )
    node_id = node.json()["data"]["judge_node_id"]
    job = claim_jobs_until(node_id, node_secret, [submission_id])[submission_id]

    detail = client.get(
        f"/api/contests/{contest_id}/submissions/{submission_id}",
        headers=auth_headers(login["access_token"]),
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == SubmissionStatus.PREPARING.value

    progress = client.post(
        f"/api/internal/judge/jobs/{job['judge_job_id']}/progress",
        json={
            "node_secret": node_secret,
            "lease_token": job["lease_token"],
            "status": SubmissionStatus.JUDGING.value,
            "progress_current": 2,
            "progress_total": 5,
        },
    )
    assert progress.status_code == 200
    assert progress.json()["data"]["submission"]["progress_current"] == 2
    assert progress.json()["data"]["submission"]["progress_total"] == 5

    waited = client.get(
        f"/api/contests/{contest_id}/submissions/{submission_id}/status:wait",
        headers=auth_headers(login["access_token"]),
    )
    assert waited.status_code == 200
    assert waited.json()["data"]["status"] == SubmissionStatus.JUDGING.value
    assert waited.json()["data"]["progress_current"] is None
    assert waited.json()["data"]["progress_total"] is None
    assert waited.json()["data"]["progress_percent"] == 40

    completed = client.post(
        f"/api/internal/judge/jobs/{job['judge_job_id']}/result",
        json={
            "node_secret": node_secret,
            "lease_token": job["lease_token"],
            "final_status": "wrong_answer",
            "runtime_ms": 123,
            "memory_kb": 4567,
        },
    )
    assert completed.status_code == 200
    assert completed.json()["data"]["submission"]["runtime_ms"] == 123
    assert completed.json()["data"]["submission"]["memory_kb"] == 4567

    participant_detail = client.get(
        f"/api/contests/{contest_id}/submissions/{submission_id}",
        headers=auth_headers(login["access_token"]),
    )
    assert participant_detail.status_code == 200
    assert participant_detail.json()["data"]["runtime_ms"] == 123
    assert participant_detail.json()["data"]["memory_kb"] == 4567


def test_participant_progress_can_be_hidden_by_contest_setting():
    contest_id, login = participant_login()
    operator = staff_tokens("test4@zoj.com")
    hidden = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={
            "participant_progress_visible": False,
            "mock_judging_progress_visible": False,
        },
    )
    assert hidden.status_code == 200
    assert hidden.json()["data"]["participant_progress_visible"] is False

    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]
    submission = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(42)"},
    )
    assert submission.status_code == 200
    submission_id = submission.json()["data"]["submission_id"]

    node_secret = "hidden-progress-secret"
    node = register_judge_node(
        json={"node_name": f"hidden-progress-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 1},
    )
    node_id = node.json()["data"]["judge_node_id"]
    job = claim_jobs_until(node_id, node_secret, [submission_id])[submission_id]
    progress = client.post(
        f"/api/internal/judge/jobs/{job['judge_job_id']}/progress",
        json={
            "node_secret": node_secret,
            "lease_token": job["lease_token"],
            "status": SubmissionStatus.JUDGING.value,
            "progress_current": 3,
            "progress_total": 5,
        },
    )
    assert progress.status_code == 200

    waited = client.get(
        f"/api/contests/{contest_id}/submissions/{submission_id}/status:wait",
        headers=auth_headers(login["access_token"]),
    )
    assert waited.status_code == 200
    assert waited.json()["data"]["status"] == SubmissionStatus.JUDGING.value
    assert waited.json()["data"]["progress_current"] is None
    assert waited.json()["data"]["progress_total"] is None
    assert waited.json()["data"]["progress_percent"] is None
    assert waited.json()["data"]["queue_position"] is None

    restored = patch_with_change_notice(
        f"/api/operator/contests/{contest_id}/settings",
        headers=auth_headers(operator["access_token"]),
        json={"participant_progress_visible": True},
    )
    assert restored.status_code == 200


def test_operator_and_admin_submission_detail_include_source_without_list_payload_bloat():
    contest_id, login = participant_login()
    set_contest_running(contest_id)
    operator = staff_tokens("test4@zoj.com")
    master = staff_tokens()
    division_id = login["division"]["division_id"]
    problems = client.get(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
    )
    assert problems.status_code == 200
    problem = next(item for item in problems.json()["data"] if item["division_id"] == division_id)
    source_code = f"print({uuid4().hex[:6]!r})"

    created = client.post(
        f"/api/contests/{contest_id}/problems/{problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": source_code},
    )
    assert created.status_code == 200
    submission_id = created.json()["data"]["submission_id"]

    operator_list = client.get(
        f"/api/operator/contests/{contest_id}/submissions",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_list.status_code == 200
    listed = next(item for item in operator_list.json()["data"] if item["submission_id"] == submission_id)
    assert listed["source_code"] is None
    operator_page = client.get(
        f"/api/operator/contests/{contest_id}/submissions?limit=1&division_id={division_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_page.status_code == 200
    assert len(operator_page.json()["data"]) <= 1
    assert operator_page.json()["page"]["limit"] == 1
    assert all(item["division_id"] == division_id for item in operator_page.json()["data"])
    operator_team_page = client.get(
        f"/api/operator/contests/{contest_id}/submissions?participant_team_id={login['team']['participant_team_id']}",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_team_page.status_code == 200
    assert operator_team_page.json()["data"]
    assert all(
        item["participant_team_id"] == login["team"]["participant_team_id"]
        for item in operator_team_page.json()["data"]
    )

    operator_detail = client.get(
        f"/api/operator/contests/{contest_id}/submissions/{submission_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_detail.status_code == 200
    assert operator_detail.json()["data"]["source_code"] == source_code

    operator_wait = client.get(
        f"/api/operator/contests/{contest_id}/submissions/{submission_id}/status:wait?wait_seconds=0",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_wait.status_code == 200
    assert operator_wait.json()["data"]["source_code"] is None

    admin_list = client.get("/api/admin/judge/submissions", headers=auth_headers(master["access_token"]))
    assert admin_list.status_code == 200
    admin_listed = next(item for item in admin_list.json()["data"] if item["submission"]["submission_id"] == submission_id)
    assert admin_listed["submission"]["source_code"] is None
    admin_page = client.get(
        f"/api/admin/judge/submissions?limit=1&contest_id={contest_id}&division_id={division_id}",
        headers=auth_headers(master["access_token"]),
    )
    assert admin_page.status_code == 200
    assert len(admin_page.json()["data"]) <= 1
    assert admin_page.json()["page"]["limit"] == 1
    assert all(
        item["submission"]["contest_id"] == contest_id
        and item["submission"]["division_id"] == division_id
        for item in admin_page.json()["data"]
    )

    admin_detail = client.get(f"/api/admin/judge/submissions/{submission_id}", headers=auth_headers(master["access_token"]))
    assert admin_detail.status_code == 200
    assert admin_detail.json()["data"]["submission"]["source_code"] == source_code

    admin_wait = client.get(
        f"/api/admin/judge/submissions/{submission_id}/status:wait?wait_seconds=0",
        headers=auth_headers(master["access_token"]),
    )
    assert admin_wait.status_code == 200
    assert admin_wait.json()["data"]["source_code"] is None

    node_secret = f"detail-secret-{uuid4().hex[:6]}"
    node = register_judge_node(
        json={"node_name": f"detail-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 10},
    )
    node_id = node.json()["data"]["judge_node_id"]
    claimed = claim_jobs_until(node_id, node_secret, [submission_id])[submission_id]
    completed = client.post(
        f"/api/internal/judge/jobs/{claimed['judge_job_id']}/result",
        json={
            "node_secret": node_secret,
            "lease_token": claimed["lease_token"],
            "final_status": "accepted",
        },
    )
    assert completed.status_code == 200


def test_submission_source_code_normalizes_line_endings_for_length():
    contest_id, login = participant_login()
    set_contest_running(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]
    problems = client.get(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
    )
    assert problems.status_code == 200
    problem = next(item for item in problems.json()["data"] if item["division_id"] == division_id)
    source_code = "print(1)\r\nprint(2)\rprint(3)"
    normalized = "print(1)\nprint(2)\nprint(3)"

    created = client.post(
        f"/api/contests/{contest_id}/problems/{problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": source_code},
    )
    assert created.status_code == 200
    submission_id = created.json()["data"]["submission_id"]

    operator_detail = client.get(
        f"/api/operator/contests/{contest_id}/submissions/{submission_id}",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_detail.status_code == 200
    assert operator_detail.json()["data"]["source_code"] == normalized

    operator_list = client.get(
        f"/api/operator/contests/{contest_id}/submissions",
        headers=auth_headers(operator["access_token"]),
    )
    assert operator_list.status_code == 200
    listed = next(item for item in operator_list.json()["data"] if item["submission_id"] == submission_id)
    assert listed["source_code_length"] == len(normalized.encode("utf-8"))


def test_participant_public_submission_list_includes_each_team_name_after_end():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]
    suffix = uuid4().hex[:8]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"N{uuid4().hex[:6]}",
            "title": "Submission Owner Names",
            "statement": "Public submission rows should show each submitting team.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 119,
        },
    )
    assert problem.status_code == 200
    problem_id = problem.json()["data"]["problem_id"]

    other_team_name = f"Other Submit Team {suffix}"
    other_email = f"other-submit-{suffix}@zoj.com"
    created_team = client.post(
        f"/api/operator/contests/{contest_id}/participants",
        headers=auth_headers(operator["access_token"]),
        json={
            "team_name": other_team_name,
            "division_id": division_id,
            "leader": {"name": "Other Submitter", "email": other_email},
            "members": [],
        },
    )
    assert created_team.status_code == 200

    set_contest_running(contest_id)
    _, other_login = participant_login(other_email)
    for token, source in [
        (login["access_token"], "print(1)"),
        (other_login["access_token"], "print(2)"),
    ]:
        response = client.post(
            f"/api/contests/{contest_id}/problems/{problem_id}/submissions",
            headers=auth_headers(token),
            json={"language": "python311", "source_code": source},
        )
        assert response.status_code == 200

    now = datetime.now(timezone.utc)
    # This ownership-list scenario uses already public, pre-freeze submissions.
    # Hidden post-freeze rows are covered by the freeze privacy regressions.
    with store._session() as db:
        for submission in db.query(SubmissionRow).filter_by(problem_id=problem_id).all():
            submission.submitted_at = now - timedelta(minutes=90)
        db.commit()
    store.update_contest_settings(
        contest_id,
        status=ContestStatus.ENDED,
        start_at=now - timedelta(hours=2),
        freeze_at=now - timedelta(hours=1),
        end_at=now - timedelta(minutes=1),
        submission_access_after_end="participants",
    )

    listed = client.get(
        f"/api/contests/{contest_id}/submissions?division_id={division_id}",
        headers=auth_headers(login["access_token"]),
    )
    assert listed.status_code == 200
    team_names = {item["team_name"] for item in listed.json()["data"] if item["problem_id"] == problem_id}
    assert login["team"]["team_name"] in team_names
    assert other_team_name in team_names


def test_problem_solve_status_uses_live_participant_results_during_scoreboard_freeze():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"F{uuid4().hex[:6]}",
            "title": "Frozen Scoreboard Live Solve Status",
            "statement": "Problem list status should not be frozen with the public scoreboard.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 119,
        },
    )
    assert problem.status_code == 200
    problem_id = problem.json()["data"]["problem_id"]
    set_contest_running(contest_id)

    wrong_submission = client.post(
        f"/api/contests/{contest_id}/problems/{problem_id}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(0)"},
    )
    assert wrong_submission.status_code == 200
    wrong_submission_data = wrong_submission.json()["data"]

    node_secret = f"freeze-status-{uuid4().hex[:6]}"
    node = register_judge_node(
        json={"node_name": f"freeze-status-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 10},
    )
    assert node.status_code == 200
    node_id = node.json()["data"]["judge_node_id"]
    wrong_job = claim_jobs_until(node_id, node_secret, [wrong_submission_data["submission_id"]])[wrong_submission_data["submission_id"]]
    wrong_result = client.post(
        f"/api/internal/judge/jobs/{wrong_job['judge_job_id']}/result",
        json={
            "node_secret": node_secret,
            "lease_token": wrong_job["lease_token"],
            "final_status": "wrong_answer",
        },
    )
    assert wrong_result.status_code == 200

    freeze_at = datetime.fromisoformat(
        wrong_submission_data["submitted_at"].replace("Z", "+00:00")
    ) + timedelta(microseconds=1)
    store.update_contest_settings(contest_id, freeze_at=freeze_at)

    accepted_submission = client.post(
        f"/api/contests/{contest_id}/problems/{problem_id}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(1)"},
    )
    assert accepted_submission.status_code == 200
    accepted_submission_id = accepted_submission.json()["data"]["submission_id"]
    accepted_job = claim_jobs_until(node_id, node_secret, [accepted_submission_id])[accepted_submission_id]
    accepted_result = client.post(
        f"/api/internal/judge/jobs/{accepted_job['judge_job_id']}/result",
        json={
            "node_secret": node_secret,
            "lease_token": accepted_job["lease_token"],
            "final_status": "accepted",
        },
    )
    assert accepted_result.status_code == 200

    frozen_scoreboard = client.get(
        f"/api/contests/{contest_id}/divisions/{division_id}/scoreboard",
        headers=auth_headers(login["access_token"]),
    )
    assert frozen_scoreboard.status_code == 200
    frozen_score = next(
        item
        for row in frozen_scoreboard.json()["data"]["rows"]
        if row["team_id"] == login["team"]["participant_team_id"]
        for item in row["problem_scores"]
        if item["problem_id"] == problem_id
    )
    assert frozen_score["solved"] is False
    assert frozen_score["attempts"] == 1

    live_problems = client.get(
        f"/api/contests/{contest_id}/divisions/{division_id}/problems",
        headers=auth_headers(login["access_token"]),
    )
    assert live_problems.status_code == 200
    problem_payload = next(
        item for item in live_problems.json()["data"] if item["problem_id"] == problem_id
    )
    assert problem_payload["solve_status"] == "accepted"

    live_scoreboard = client.get(
        f"/api/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/internal",
        headers=auth_headers(operator["access_token"]),
    )
    assert live_scoreboard.status_code == 200
    problem_stat = next(
        item
        for item in live_scoreboard.json()["data"]["problem_stats"]
        if item["problem_id"] == problem_id
    )
    assert problem_stat["total_submissions"] == 2
    assert problem_stat["accepted_submissions"] == 1
    assert problem_stat["accepted_team_count"] == 1
    assert problem_stat["total_team_count"] == len(live_scoreboard.json()["data"]["rows"])
    assert problem_stat["acceptance_rate"] == 50.0
    assert problem_stat["first_accepted_team_id"] == login["team"]["participant_team_id"]
    assert problem_stat["first_accepted_team_name"] == login["team"]["team_name"]
    assert isinstance(problem_stat["first_accepted_elapsed_minutes"], int)


def test_scoreboard_uses_icpc_attempt_policy_per_problem():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"S{uuid4().hex[:6]}",
            "title": "ICPC Attempt Policy",
            "statement": "Scoreboard should use solved count and penalty, not partial scores.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 120,
        },
    ).json()["data"]
    set_contest_running(contest_id)

    submissions = []
    for source_code in ["print(30)", "print(20)", "print(10)", "print(5)", "broken"]:
        response = client.post(
            f"/api/contests/{contest_id}/problems/{problem['problem_id']}/submissions",
            headers=auth_headers(login["access_token"]),
            json={"language": "python311", "source_code": source_code},
        )
        assert response.status_code == 200
        submissions.append(response.json()["data"])

    node_secret = "score-secret"
    node = register_judge_node(
        json={"node_name": f"score-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 10},
    )
    node_id = node.json()["data"]["judge_node_id"]
    jobs_by_submission = claim_jobs_until(node_id, node_secret, [submission["submission_id"] for submission in submissions])

    results = [
        (submissions[0]["submission_id"], "wrong_answer"),
        (submissions[1]["submission_id"], "runtime_error"),
        (submissions[2]["submission_id"], "time_limit_exceeded"),
        (submissions[3]["submission_id"], "system_error"),
        (submissions[4]["submission_id"], "compile_error"),
    ]
    for submission_id, status in results:
        job = jobs_by_submission[submission_id]
        response = client.post(
            f"/api/internal/judge/jobs/{job['judge_job_id']}/result",
            json={
                "node_secret": node_secret,
                "lease_token": job["lease_token"],
                "final_status": status,
            },
        )
        assert response.status_code == 200

    scoreboard = client.get(
        f"/api/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/internal",
        headers=auth_headers(operator["access_token"]),
    )
    assert scoreboard.status_code == 200
    team_row = next(row for row in scoreboard.json()["data"]["rows"] if row["team_id"] == login["team"]["participant_team_id"])
    problem_score = next(item for item in team_row["problem_scores"] if item["problem_id"] == problem["problem_id"])
    assert "score" not in problem_score
    assert problem_score["best_submission_id"] is None
    assert problem_score["attempts"] == 3
    assert problem_score["wrong_attempts"] == 3
    assert problem_score["solved"] is False


    retry_id = submissions[3]["submission_id"]
    retried = client.post(f"/api/operator/contests/{contest_id}/submissions/{retry_id}/rejudge", headers=auth_headers(operator["access_token"]))
    assert retried.status_code == 200
    assert retried.json()["data"]["submitted_at"] == submissions[3]["submitted_at"]
    assert retried.json()["data"]["source_code"] == "print(5)"
    job = claim_jobs_until(node_id, node_secret, [retry_id])[retry_id]
    result = client.post(f"/api/internal/judge/jobs/{job['judge_job_id']}/result", json={"node_secret": node_secret, "lease_token": job["lease_token"], "final_status": "accepted"})
    assert result.status_code == 200
    board = client.get(f"/api/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/internal", headers=auth_headers(operator["access_token"])).json()["data"]
    team = next(row for row in board["rows"] if row["team_id"] == login["team"]["participant_team_id"])
    score = next(item for item in team["problem_scores"] if item["problem_id"] == problem["problem_id"])
    assert score["solved"] is True
    assert score["wrong_attempts"] == 3
    assert score["best_submission_id"] == retry_id

def test_scoreboard_ties_zero_solve_and_orders_wrong_only_below_no_submit():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]
    suffix = uuid4().hex[:8]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"T{uuid4().hex[:6]}",
            "title": "Zero Solve Tie Policy",
            "statement": "Scoreboard should tie no-submit teams and rank wrong-only teams below.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 121,
        },
    )
    assert problem.status_code == 200
    problem_id = problem.json()["data"]["problem_id"]

    teams = []
    for label in ["No Submit A", "No Submit B", "Wrong Only"]:
        email = f"scoreboard-{label.lower().replace(' ', '-')}-{suffix}@zoj.com"
        created = client.post(
            f"/api/operator/contests/{contest_id}/participants",
            headers=auth_headers(operator["access_token"]),
            json={
                "team_name": f"{label} {suffix}",
                "division_id": division_id,
                "leader": {"name": label, "email": email},
                "members": [],
            },
        )
        assert created.status_code == 200
        teams.append((label, email, created.json()["data"]))

    set_contest_running(contest_id)
    _, wrong_login = participant_login(teams[2][1])
    wrong_submission = client.post(
        f"/api/contests/{contest_id}/problems/{problem_id}/submissions",
        headers=auth_headers(wrong_login["access_token"]),
        json={"language": "python311", "source_code": "print(0)"},
    )
    assert wrong_submission.status_code == 200
    submission_id = wrong_submission.json()["data"]["submission_id"]

    node_secret = f"rank-secret-{uuid4().hex[:6]}"
    node = register_judge_node(
        json={"node_name": f"rank-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 10},
    )
    assert node.status_code == 200
    node_id = node.json()["data"]["judge_node_id"]
    job = claim_jobs_until(node_id, node_secret, [submission_id])[submission_id]
    judged = client.post(
        f"/api/internal/judge/jobs/{job['judge_job_id']}/result",
        json={
            "node_secret": node_secret,
            "lease_token": job["lease_token"],
            "final_status": "wrong_answer",
        },
    )
    assert judged.status_code == 200

    scoreboard = client.get(
        f"/api/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/internal",
        headers=auth_headers(operator["access_token"]),
    )
    assert scoreboard.status_code == 200
    rows_by_team = {row["team_id"]: row for row in scoreboard.json()["data"]["rows"]}
    no_submit_a = rows_by_team[teams[0][2]["participant_team_id"]]
    no_submit_b = rows_by_team[teams[1][2]["participant_team_id"]]
    wrong_only = rows_by_team[teams[2][2]["participant_team_id"]]

    assert no_submit_a["solved"] == no_submit_b["solved"] == wrong_only["solved"] == 0
    assert no_submit_a["penalty"] == no_submit_b["penalty"] == wrong_only["penalty"] == 0
    assert no_submit_a["rank"] == no_submit_b["rank"]
    assert no_submit_a["rank"] < wrong_only["rank"]


def test_manual_rejudge_api_is_not_available_to_service_master_or_operator():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    master = staff_tokens("test3@zoj.com")
    division_id = login["division"]["division_id"]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"R{uuid4().hex[:6]}",
            "title": "Rejudge Policy",
            "statement": "Manual rejudge API must not be exposed.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 130,
        },
    ).json()["data"]
    set_contest_running(contest_id)
    submission = client.post(
        f"/api/contests/{contest_id}/problems/{problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(90)"},
    ).json()["data"]

    node_secret = "rejudge-secret"
    node = register_judge_node(
        json={"node_name": f"rejudge-node-{uuid4().hex[:6]}", "node_secret": node_secret, "total_slots": 10},
    )
    node_id = node.json()["data"]["judge_node_id"]
    claim = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": node_secret, "max_count": 100},
    )
    job = next(job for job in claim.json()["data"]["jobs"] if job["submission"]["submission_id"] == submission["submission_id"])
    result = client.post(
        f"/api/internal/judge/jobs/{job['judge_job_id']}/result",
        json={
            "node_secret": node_secret,
            "lease_token": job["lease_token"],
            "final_status": "wrong_answer",
        },
    )
    assert result.status_code == 200

    denied = client.post(
        f"/api/admin/contests/{contest_id}/submissions/{submission['submission_id']}/rejudge",
        headers=auth_headers(operator["access_token"]),
    )
    assert denied.status_code == 404

    master_denied = client.post(
        f"/api/admin/contests/{contest_id}/submissions/{submission['submission_id']}/rejudge",
        headers=auth_headers(master["access_token"]),
    )
    assert master_denied.status_code == 404

    scoreboard = client.get(
        f"/api/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/internal",
        headers=auth_headers(operator["access_token"]),
    )
    problem_score = next(
        item
        for row in scoreboard.json()["data"]["rows"]
        if row["team_id"] == login["team"]["participant_team_id"]
        for item in row["problem_scores"]
        if item["problem_id"] == problem["problem_id"]
    )
    assert "score" not in problem_score
    assert problem_score["best_submission_id"] is None
    assert problem_score["attempts"] == 1
    assert problem_score["wrong_attempts"] == 1
    assert problem_score["solved"] is False


def test_judge_claim_includes_active_testcases():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"T{uuid4().hex[:6]}",
            "title": "Claim Testcases",
            "statement": "Use active testcase set.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "language_resource_limits": {
                "python311": {"time_limit_ms": 2500, "memory_limit_mb": 768}
            },
            "display_order": 99,
        },
    ).json()["data"]
    assert problem["language_resource_limits"]["python311"]["time_limit_ms"] == 2500
    testcase_set = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
        json={"is_active": True},
    ).json()["data"]
    client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{testcase_set['testcase_set_id']}/testcases",
        headers=auth_headers(operator["access_token"]),
        json={
            "display_order": 1,
            "input_storage_key": f"contests/{contest_id}/local/input.txt",
            "output_storage_key": f"contests/{contest_id}/local/output.txt",
            "input_sha256": "d" * 64,
            "output_sha256": "e" * 64,
        },
    )
    set_contest_running(contest_id)
    submission = client.post(
        f"/api/contests/{contest_id}/problems/{problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(42)"},
    )
    assert submission.status_code == 200
    submission_id = submission.json()["data"]["submission_id"]

    node = register_judge_node(
        json={"node_name": f"pytest-node-{uuid4().hex[:6]}", "node_secret": "demo", "total_slots": 1},
    )
    node_id = node.json()["data"]["judge_node_id"]
    claim = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": "demo", "max_count": 20},
    )
    assert claim.status_code == 200
    job = next(item for item in claim.json()["data"]["jobs"] if item["submission"]["submission_id"] == submission_id)
    assert job["testcase_set"]["testcase_set_id"] == testcase_set["testcase_set_id"]
    assert job["testcases"][0]["input_storage_key"] == f"contests/{contest_id}/local/input.txt"
    assert job["testcases"][0]["input_url"].startswith("file://")
    assert job["problem"]["problem_id"] == problem["problem_id"]
    assert job["problem"]["time_limit_ms"] == 1000
    assert job["problem"]["language_resource_limits"]["python311"] == {
        "time_limit_ms": 2500,
        "memory_limit_mb": 768,
    }
    assert job["leased_at"]

    empty_claim = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": "demo", "max_count": 20, "wait_seconds": 0.01},
    )
    assert empty_claim.status_code == 200
    assert empty_claim.json()["data"]["jobs"] == []


def test_judge_claim_uses_compact_payload_when_bundle_exists():
    contest_id, login = participant_login()
    set_contest_mutable(contest_id)
    operator = staff_tokens("test4@zoj.com")
    division_id = login["division"]["division_id"]

    problem = client.post(
        f"/api/operator/contests/{contest_id}/problems",
        headers=auth_headers(operator["access_token"]),
        json={
            "division_id": division_id,
            "problem_code": f"B{uuid4().hex[:6]}",
            "title": "Bundled Claim Testcases",
            "statement": "Use bundled testcase payload.",
            "time_limit_ms": 1000,
            "memory_limit_mb": 512,
            "display_order": 100,
        },
    ).json()["data"]
    testcase_set = client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets",
        headers=auth_headers(operator["access_token"]),
        json={"is_active": True},
    ).json()["data"]
    input_key = f"contests/{contest_id}/problems/{problem['problem_id']}/testcases/{uuid4().hex}.in"
    output_key = f"contests/{contest_id}/problems/{problem['problem_id']}/testcases/{uuid4().hex}.out"
    object_storage.write_text(input_key, "40 2\n")
    object_storage.write_text(output_key, "42\n")
    client.post(
        f"/api/operator/contests/{contest_id}/problems/{problem['problem_id']}/testcase-sets/{testcase_set['testcase_set_id']}/testcases",
        headers=auth_headers(operator["access_token"]),
        json={
            "display_order": 1,
            "input_storage_key": input_key,
            "output_storage_key": output_key,
            "input_sha256": hashlib.sha256(b"40 2\n").hexdigest(),
            "output_sha256": hashlib.sha256(b"42\n").hexdigest(),
        },
    )
    assert store.warm_problem_judge_bundle(contest_id, problem["problem_id"])

    set_contest_running(contest_id)
    submission = client.post(
        f"/api/contests/{contest_id}/problems/{problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(sum(map(int, input().split())))"},
    )
    assert submission.status_code == 200

    node = register_judge_node(
        json={"node_name": f"pytest-node-{uuid4().hex[:6]}", "node_secret": "demo", "total_slots": 1},
    )
    claim = client.post(
        f"/api/internal/judge/nodes/{node.json()['data']['judge_node_id']}/assignments:claim",
        json={"node_secret": "demo", "max_count": 1},
    )
    assert claim.status_code == 200
    job = claim.json()["data"]["jobs"][0]
    assert job["bundle_url"]
    assert job["testcases"][0]["input_storage_key"] == input_key
    assert job["testcases"][0]["input_url"].startswith("file://")
    assert job["testcases"][0]["output_url"].startswith("file://")
    assert "input_text" not in job["testcases"][0]
    assert "output_text" not in job["testcases"][0]


def test_judge_dispatcher_recovers_expired_leases():
    contest_id, login = participant_login()
    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]

    submission = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(42)"},
    ).json()["data"]
    node = register_judge_node(
        json={"node_name": f"expired-node-{uuid4().hex[:6]}", "node_secret": "demo", "total_slots": 1},
    )
    node_id = node.json()["data"]["judge_node_id"]
    claim = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": "demo", "max_count": 1},
    )
    job = claim.json()["data"]["jobs"][0]

    with store._session() as db:
        row = db.get(JudgeJobRow, job["judge_job_id"])
        row.leased_at = datetime.now(timezone.utc) - timedelta(seconds=180)
        db.commit()

    recovered = client.post(
        f"/api/internal/judge/nodes/{node_id}/assignments:claim",
        json={"node_secret": "demo", "max_count": 1},
    )
    assert recovered.status_code == 200
    recovered_job = recovered.json()["data"]["jobs"][0]
    assert recovered_job["judge_job_id"] == job["judge_job_id"]
    assert recovered_job["lease_token"] != job["lease_token"]

    with store._session() as db:
        submission_row = db.get(SubmissionRow, submission["submission_id"])
        job_row = db.get(JudgeJobRow, job["judge_job_id"])
        assert submission_row.status == SubmissionStatus.PREPARING.value
        assert job_row.status == JudgeJobStatus.RUNNING.value


def test_participant_submission_requires_token_and_filters_team():
    contest_id, login = participant_login()
    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]

    denied = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        json={"language": "cpp17", "source_code": "int main(){return 0;}"},
    )
    assert denied.status_code == 401

    submission = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "cpp17", "source_code": "int main(){return 0;}"},
    )
    assert submission.status_code == 200
    submission_id = submission.json()["data"]["submission_id"]

    own_list = client.get(f"/api/contests/{contest_id}/submissions", headers=auth_headers(login["access_token"]))
    assert own_list.status_code == 200
    assert all(item["participant_team_id"] == login["team"]["participant_team_id"] for item in own_list.json()["data"])
    own_page = client.get(f"/api/contests/{contest_id}/submissions?limit=1", headers=auth_headers(login["access_token"]))
    assert own_page.status_code == 200
    assert len(own_page.json()["data"]) <= 1
    assert own_page.json()["page"]["limit"] == 1

    detail = client.get(f"/api/contests/{contest_id}/submissions/{submission_id}", headers=auth_headers(login["access_token"]))
    assert detail.status_code == 200


def test_submission_status_wait_does_not_auto_accept_without_judge_result():
    contest_id, login = participant_login()
    advanced_division = login["division"]
    advanced_problem = client.get(
        f"/api/contests/{contest_id}/divisions/{advanced_division['division_id']}/problems",
        headers=auth_headers(login["access_token"]),
    ).json()["data"][0]

    submission = client.post(
        f"/api/contests/{contest_id}/problems/{advanced_problem['problem_id']}/submissions",
        headers=auth_headers(login["access_token"]),
        json={"language": "python311", "source_code": "print(42)"},
    )
    assert submission.status_code == 200
    submission_id = submission.json()["data"]["submission_id"]

    waited = client.get(
        f"/api/contests/{contest_id}/submissions/{submission_id}/status:wait",
        headers=auth_headers(login["access_token"]),
    )
    assert waited.status_code == 200
    assert waited.json()["data"]["status"] == SubmissionStatus.WAITING.value

    detail = client.get(
        f"/api/contests/{contest_id}/submissions/{submission_id}",
        headers=auth_headers(login["access_token"]),
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == SubmissionStatus.WAITING.value
