"""Login-email updates preserve staff identity and revoke old authentication."""
import json
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import (
    GeneralSessionRow, MailQueueItemRow, OtpCodeRow, StaffAccountRow, TeamMemberRow,
)
from app.services.store import store
from tests.judging_fixtures import ensure_judging_inputs

client = TestClient(app)


def login(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def headers(session):
    return {"Authorization": "Bearer " + session["access_token"]}


def fresh_email(prefix="staff"):
    return f"{prefix}-{uuid4().hex}@zoj.com"


@pytest.fixture
def context():
    contest = store.create_contest("Email changes", "Test", "Test", now_utc() + timedelta(days=1), status=ContestStatus.DRAFT)
    cid = contest.contest_id
    store.upsert_contest_operator(cid, f"owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    division = store.create_contest_division(cid, "A", "A")
    problem = store.create_problem(cid, division.division_id, "A", "Problem", "Statement", 1000, 128, {}, 1)
    ensure_judging_inputs(problem.problem_id)
    actor = store.upsert_contest_operator(cid, fresh_email("actor"), "Manager", ["staff_manager"])
    target = store.upsert_contest_operator(cid, fresh_email("target"), "Original name", ["problem_reviewer"])
    return {"cid": cid, "division": division, "problem": problem, "actor": actor, "target": target, "actor_session": login(str(actor.email)), "target_session": login(str(target.email)), "base": f"/api/operator/contests/{cid}"}


def patch(c, email, *, roles=None, name="Changed name", actor_session=None, old_email=None):
    return client.patch(c["base"] + "/operators/" + (old_email or str(c["target"].email)), headers=headers(actor_session or c["actor_session"]), json={"email": email, "display_name": name, "roles": roles or ["problem_reviewer"]})


def unchanged(c):
    with store._session() as db:
        row = db.get(StaffAccountRow, c["target"].staff_account_id)
        assert row.email == str(c["target"].email)
        assert row.display_name == c["target"].display_name
        assert json.loads(row.contest_roles)[c["cid"]] == c["target"].contest_roles[c["cid"]]
    assert client.get("/api/auth/general/me", headers=headers(c["target_session"])).status_code == 200


def test_email_update_preserves_id_submissions_authorship_and_invalidates_old_login(context):
    c = context
    old_email = str(c["target"].email)
    new_email = fresh_email("new")
    original_submission = store.create_operator_test_submission(c["cid"], c["problem"].problem_id, "cpp17", "int main(){}", submitted_by_name=c["target"].display_name, submitted_by_email=old_email)
    real_email = fresh_email("real")
    team = store.create_participant_team(c["cid"], c["division"].division_id, "Real", "Real", real_email, [])
    participant = {"team": team, "member": team.members[0], "division": c["division"]}
    question = store.create_question(c["cid"], participant, "Question", "Body", "public")
    answer = store.create_answer(c["cid"], question.contest_question_id, "Staff answer", "public", old_email)
    notice = store.create_contest_notice(c["cid"], "Notice", "Body", created_by_email=old_email)
    with store._session() as db:
        target_row = db.get(StaffAccountRow, c["target"].staff_account_id)
        staff_session = store._issue_staff_session(db, target_row)
        for email in [old_email, new_email]:
            db.add(OtpCodeRow(email=email, contest_id="__general__", code="123456", expires_at=now_utc() + timedelta(minutes=5)))
            db.add(MailQueueItemRow(mail_type="general_otp", recipient_email=email, subject="OTP", body_text="123456", status="pending"))
        db.commit()
    response = patch(c, "  " + new_email.upper() + "  ", roles=["problem_author"], name="New display name")
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["email"] == new_email
    assert updated["staff_account_id"] == c["target"].staff_account_id
    assert updated["display_name"] == "New display name"
    assert updated["contest_roles"][c["cid"]] == ["problem_author"]
    assert client.get("/api/auth/general/me", headers=headers(c["target_session"])).status_code == 401
    assert client.post("/api/auth/general/refresh", json={"refresh_token": c["target_session"]["refresh_token"]}).status_code == 401
    assert client.get("/api/auth/staff/me", headers=headers(staff_session)).status_code == 401
    assert client.post("/api/auth/staff/refresh", json={"refresh_token": staff_session["refresh_token"]}).status_code == 401
    assert client.post("/api/auth/general/otp/verify", json={"email": old_email, "otp_code": "123456", "force_new_session": True}).status_code == 401
    assert client.post("/api/auth/general/otp/verify", json={"email": new_email, "otp_code": "123456", "force_new_session": True}).status_code == 401
    current = login(new_email)
    submission = client.get(c["base"] + "/test-submissions/" + original_submission.submission_id, headers=headers(current))
    assert submission.status_code == 200
    assert submission.json()["data"]["submitted_by_email"] == new_email
    assert submission.json()["data"]["submitted_by_name"] == c["target"].display_name
    assert submission.json()["data"]["submitted_by_title"] == "출제진"
    updated_question = store.get_contest_question(c["cid"], question.contest_question_id)
    saved_answer = next(item for item in updated_question.answers if item.contest_answer_id == answer.contest_answer_id)
    assert saved_answer.created_by_email == new_email
    assert saved_answer.created_by_name == "New display name"
    assert saved_answer.created_by_title == "출제진"
    assert store.contest_notices[notice.contest_notice_id].created_by_email == new_email
    with store._session() as db:
        assert db.get(OtpCodeRow, old_email) is None and db.get(OtpCodeRow, new_email) is None
        assert all(row.status == "cancelled" for row in db.scalars(select(MailQueueItemRow).where(MailQueueItemRow.recipient_email.in_([old_email, new_email]), MailQueueItemRow.mail_type == "general_otp")))
        assert all(row.revoked_at for row in db.scalars(select(GeneralSessionRow).where(GeneralSessionRow.email == old_email)))
    # Re-registering the retired address never restores the old browser's tokens
    # or assigns the renamed account's existing test submissions to the new account.
    replacement = store.upsert_contest_operator(c["cid"], old_email, "Another person", ["problem_reviewer"])
    assert replacement.staff_account_id != c["target"].staff_account_id
    assert client.get("/api/auth/general/me", headers=headers(c["target_session"])).status_code == 401
    assert client.get(c["base"] + "/test-submissions/" + original_submission.submission_id, headers=headers(login(old_email))).status_code == 403


def test_same_normalized_email_is_a_nonrevoking_update(context):
    c = context
    response = patch(c, "  " + str(c["target"].email).upper() + "  ")
    assert response.status_code == 200
    assert response.json()["data"]["email"] == str(c["target"].email)
    assert client.get("/api/auth/general/me", headers=headers(c["target_session"])).status_code == 200
    assert client.post("/api/auth/general/refresh", json={"refresh_token": c["target_session"]["refresh_token"]}).status_code == 200


@pytest.mark.parametrize("include_email", [False, True])
def test_name_role_update_keeps_existing_same_contest_participant_guard(context, include_email):
    c = context
    team = store.create_participant_team(c["cid"], c["division"].division_id, "Legacy overlap", "Participant", fresh_email("participant"), [])
    with store._session() as db:
        db.get(TeamMemberRow, team.members[0].team_member_id).email = str(c["target"].email)
        db.commit()
    payload = {"display_name": "Must not persist", "roles": ["notices_manager"]}
    if include_email:
        payload["email"] = str(c["target"].email)
    response = client.patch(c["base"] + "/operators/" + str(c["target"].email), headers=headers(c["actor_session"]), json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_change_participant_identity"
    unchanged(c)


@pytest.mark.parametrize("collision", ["staff", "participant", "general", "historical_author"])
def test_email_collision_is_atomic_and_keeps_original_session(context, collision):
    c = context
    new_email = fresh_email("occupied")
    if collision == "staff":
        store.upsert_contest_operator(c["cid"], new_email, "Other staff", ["notices_manager"])
    elif collision == "participant":
        foreign = store.create_contest("Other", "Other", "Other")
        division = store.create_contest_division(foreign.contest_id, "B", "B")
        store.create_participant_team(foreign.contest_id, division.division_id, "Other team", "Other", new_email, [])
    elif collision == "general":
        with store._session() as db:
            db.add(GeneralSessionRow(email=new_email, access_token_hash=uuid4().hex, refresh_token_hash=uuid4().hex, access_expires_at=now_utc() - timedelta(days=1), refresh_expires_at=now_utc() - timedelta(days=1), revoked_at=now_utc()))
            db.commit()
    else:
        store.create_operator_test_submission(c["cid"], c["problem"].problem_id, "cpp17", "int main(){}", submitted_by_email=new_email, submitted_by_name="Historical author")
    response = patch(c, new_email, roles=["notices_manager"], name="Should not persist")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_in_use"
    unchanged(c)


def test_cross_contest_staff_and_master_permissions_are_required(context):
    c = context
    other = store.create_contest("Other scope", "Other", "Other")
    store.upsert_contest_operator(other.contest_id, fresh_email("owner"), "Owner", ["master"])
    store.upsert_contest_operator(other.contest_id, str(c["target"].email), c["target"].display_name, ["master"])
    destination = fresh_email("global")
    denied = patch(c, destination)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "email_change_scope_denied"
    unchanged(c)
    store.upsert_contest_operator(other.contest_id, str(c["actor"].email), c["actor"].display_name, ["staff_manager"])
    assert patch(c, destination).status_code == 403
    store.upsert_contest_operator(other.contest_id, str(c["actor"].email), c["actor"].display_name, ["master"])
    allowed = patch(c, destination)
    assert allowed.status_code == 200
    with store._session() as db:
        target = db.get(StaffAccountRow, c["target"].staff_account_id)
        assert json.loads(target.contest_roles)[other.contest_id] == ["master"]
        assert json.loads(target.contest_scopes)[other.contest_id] == ["contest.*"]


def test_protected_master_email_needs_service_master_even_in_another_contest(context):
    c = context
    other = store.create_contest("Protected scope", "Other", "Other")
    store.upsert_contest_operator(other.contest_id, str(c["target"].email), c["target"].display_name, protected_master=True)
    store.upsert_contest_operator(other.contest_id, str(c["actor"].email), c["actor"].display_name, ["master"])
    destination = fresh_email("protected-new")
    denied = patch(c, destination)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "assigned_master_email_immutable"
    unchanged(c)
    response = patch(c, destination, actor_session=login("test3@zoj.com"))
    assert response.status_code == 200
    with store._session() as db:
        row = db.get(StaffAccountRow, c["target"].staff_account_id)
        assert other.contest_id in json.loads(row.protected_master_contests)
        assert json.loads(row.contest_roles)[other.contest_id] == ["owner"]


def test_old_participant_identity_and_its_answers_cannot_be_transferred(context):
    c = context
    old_email = str(c["target"].email)
    foreign = store.create_contest("Participant scope", "Other", "Other")
    division = store.create_contest_division(foreign.contest_id, "B", "B")
    # This is a legacy identity combination supported by old operator assignment:
    # the participant existed first, then gained staff rights in another contest.
    with store._session() as db:
        db.get(StaffAccountRow, c["target"].staff_account_id).email = fresh_email("temporary")
        db.commit()
    team = store.create_participant_team(foreign.contest_id, division.division_id, "Participant team", "Participant name", old_email, [])
    with store._session() as db:
        db.get(StaffAccountRow, c["target"].staff_account_id).email = old_email
        db.commit()
    question = store.create_question(foreign.contest_id, {"team": team, "member": team.members[0], "division": division}, "Participant question", "Body", "public")
    answer = store.create_answer(foreign.contest_id, question.contest_question_id, "Participant reply", "public", old_email)
    denied = patch(c, fresh_email("forbidden-transfer"), roles=["notices_manager"])
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "email_change_participant_identity"
    unchanged(c)
    updated_answer = store.get_contest_question(foreign.contest_id, question.contest_question_id).answers[0]
    assert updated_answer.contest_answer_id == answer.contest_answer_id
    assert updated_answer.created_by_email == old_email
    assert updated_answer.created_by_role == "participant"
    assert updated_answer.created_by_name == "Participant name"
    with store._session() as db:
        assert db.get(TeamMemberRow, team.members[0].team_member_id).email == old_email


def test_preview_history_follows_stable_staff_identity_and_needs_fresh_selection(context):
    c = context
    old_email = str(c["target"].email)
    store.update_contest_operator(c["cid"], old_email, c["target"].display_name, ["participant_preview"])
    auth = f"/api/auth/general/contests/{c['cid']}"
    participant = f"/api/contests/{c['cid']}"
    session = c["target_session"]
    assert client.post(auth + "/participant-preview-session", headers=headers(session), json={"division_id": c["division"].division_id}).status_code == 200
    submission = client.post(participant + f"/problems/{c['problem'].problem_id}/submissions", headers=headers(session), json={"language": "cpp17", "source_code": "int main(){}"}).json()["data"]
    question = client.post(participant + "/boards", headers=headers(session), json={"title": "Preview", "body": old_email}).json()["data"]
    assert client.post(participant + f"/boards/{question['contest_question_id']}/answers", headers=headers(session), json={"body": "Reply"}).status_code == 200
    new_email = fresh_email("preview-renamed")
    changed = patch(c, new_email, roles=["participant_preview"])
    assert changed.status_code == 200, changed.text
    assert client.get(participant + "/participant-session/me", headers=headers(session)).status_code == 401
    fresh = login(new_email)
    assert client.get(auth + "/participant-preview", headers=headers(fresh)).json()["data"]["selected_division_id"] is None
    assert client.post(auth + "/participant-preview-session", headers=headers(fresh), json={"division_id": c["division"].division_id}).status_code == 200
    history = client.get(participant + "/submissions?include_source=true", headers=headers(fresh)).json()["data"]
    assert history[0]["submission_id"] == submission["submission_id"]
    assert history[0]["submitted_by_email"] == new_email
    assert history[0]["source_code"] == "int main(){}"
    board = client.get(participant + "/boards", headers=headers(fresh)).json()["data"]
    assert board[0]["contest_question_id"] == question["contest_question_id"]
    assert board[0]["author_email"] == new_email
    assert board[0]["answers"][0]["created_by_email"] == new_email
    assert board[0]["body"] == old_email  # User content is never rewritten.


def test_self_email_change_returns_success_then_revokes_and_keeps_audit_actor(context):
    c = context
    old_email = str(c["actor"].email)
    new_email = fresh_email("self")
    response = patch(c, new_email, old_email=old_email, roles=["staff_manager"], name="Changed self")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["email"] == new_email
    assert client.get("/api/auth/general/me", headers=headers(c["actor_session"])).status_code == 401
    with store._session() as db:
        from app.orm_models import OperationalAuditLogRow
        log = db.scalar(select(OperationalAuditLogRow).where(OperationalAuditLogRow.path == c["base"] + "/operators/" + old_email, OperationalAuditLogRow.method == "PATCH").order_by(OperationalAuditLogRow.created_at.desc()))
        assert log.status_code == 200
        assert log.actor_email == old_email
        assert log.actor_name == "Manager"
    assert login(new_email)["account"]["display_name"] == "Changed self"
