import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.database import engine
from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import ParticipantTeamRow, StaffSessionRow, GeneralSessionRow
from app.services import participant_import
from app.services.store import store


client = TestClient(app)


def login(email="test3@zoj.com"):
    response = client.post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.fixture
def context():
    session = login()
    contest = store.create_contest("Import " + uuid4().hex, "Test", "Test", now_utc() + timedelta(days=2), status=ContestStatus.DRAFT)
    division = store.create_contest_division(contest.contest_id, "general", "General")
    return {"cid": contest.contest_id, "division": division.division_id,
        "headers": {"Authorization": "Bearer " + session["operator_session"]["access_token"]},
        "general_headers": {"Authorization": "Bearer " + session["access_token"]},
        "path": f"/api/operator/contests/{contest.contest_id}/participants:bulk-"}


def row(context, name="Team", email=None, **extra):
    return {"team_name": name, "division_id": context["division"],
        "leader": {"name": "Leader", "email": email or f"person-{uuid4().hex}@example.com"}, "members": [], **extra}


def request(context, action, rows, headers=None):
    return client.post(context["path"] + action, headers=headers or context["headers"], json={"teams": rows})


@pytest.mark.parametrize("general", [False, True])
def test_preview_is_read_only_including_auth_activity_and_audit(context, general):
    with store._session() as db:
        for model in (StaffSessionRow, GeneralSessionRow):
            for session in db.scalars(select(model)):
                session.last_seen_at = None
        db.commit()
    submitted = row(context, name="  Trimmed Team  ", email="  Canonical@EXAMPLE.COM  ")
    submitted["leader"]["name"] = "  Person  "
    writes = []
    def collect(conn, cursor, statement, parameters, execution, many):
        if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)
    event.listen(engine, "before_cursor_execute", collect)
    try:
        response = request(context, "preview", [submitted], context["general_headers"] if general else None)
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert response.status_code == 200, response.text
    assert writes == []
    data = response.json()["data"]
    assert data["summary"] == {"total": 1, "ready": 1, "invalid": 0, "corrected": 1, "warnings": 0}
    checked = data["rows"][0]
    assert checked["input"] == submitted
    assert checked["normalized"]["team_name"] == "Trimmed Team"
    assert checked["normalized"]["leader"] == {"name": "Person", "email": "canonical@example.com"}
    assert {correction["field"] for correction in checked["corrections"]} == {"team_name", "leader.name", "leader.email"}
    edited = copy.deepcopy(checked["normalized"])
    edited["team_name"] = "Edited before confirmation"
    edited["leader"]["name"] = "Edited leader"
    assert request(context, "preview", [edited]).json()["data"]["rows"][0]["normalized"] == edited
    saved = request(context, "create", [edited])
    assert saved.status_code == 200
    assert saved.json()["data"]["created"][0]["team_name"] == edited["team_name"]


def test_preview_reports_all_row_conflicts_and_does_not_reserve_invalid_rows(context):
    email = f"duplicate-{uuid4().hex}@example.com"
    accepted = row(context, "First", email)
    within = row(context, "Within", f"within-{uuid4().hex}@example.com")
    within["members"] = [{"name": "Member", "email": within["leader"]["email"].upper()}]
    invalid_division = row(context, "Bad division", division_id=str(uuid4()))
    rows = [accepted, row(context, "First"), row(context, "Repeated email", email), within,
        invalid_division, row(context, "Staff", "test3@zoj.com"), row(context, "Missing", leader={"name": "", "email": "not-an-email"}),
        row(context, "Retry invalid row email", invalid_division["leader"]["email"])]
    response = request(context, "preview", rows)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"]["ready"] == 2
    assert data["summary"]["invalid"] == 6
    assert [item["row"] for item in data["rows"]] == list(range(1, 9))
    codes = [{error["code"] for error in item["errors"]} for item in data["rows"]]
    assert codes[1] == {"duplicate_team_name"}
    assert codes[2] == {"duplicate_email"}
    assert codes[3] == {"duplicate_email"}
    assert codes[4] == {"invalid_division"}
    assert codes[5] == {"staff_email_conflict"}
    assert codes[6] == {"required", "invalid_email"}
    assert data["rows"][7]["valid"] is True


@pytest.mark.parametrize("all_invalid", [False, True])
def test_create_returns_partial_or_all_failures_without_whole_batch_pydantic_error(context, all_invalid):
    bad = row(context, "Malformed email", leader={"name": "Bad", "email": "invalid-address"})
    rows = [bad, None] if all_invalid else [row(context, "Good before"), bad, row(context, "Good after")]
    response = request(context, "create", rows)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"] == {"total": len(rows), "created": 0 if all_invalid else 2, "failed": 2 if all_invalid else 1, "warnings": 0}
    assert data["errors"][0]["input"] == bad
    assert data["errors"][0]["code"] == "invalid_email"
    assert data["errors"][0]["normalized"] is None
    assert [result["row"] for result in data["results"]] == list(range(1, len(rows) + 1))


def test_final_creation_rechecks_database_after_preview(context):
    submitted = row(context, "Previewed")
    assert request(context, "preview", [submitted]).json()["data"]["rows"][0]["valid"]
    store.create_participant_team(context["cid"], context["division"], "Concurrent team", "Other", submitted["leader"]["email"], [])
    response = request(context, "create", [submitted, row(context, "Still valid")])
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["summary"] == {"total": 2, "created": 1, "failed": 1, "warnings": 0}
    assert data["errors"][0]["row"] == 1 and data["errors"][0]["code"] == "duplicate_email"


def test_integrity_race_rolls_back_only_failed_row_and_keeps_other_successes(context, monkeypatch):
    store.create_participant_team(context["cid"], context["division"], "Race duplicate", "Existing", f"existing-{uuid4().hex}@example.com", [])
    real_validate = participant_import.creation_issues
    bypassed = False
    def validation(state, division_id, team_name, fields):
        nonlocal bypassed
        if team_name == "Race duplicate" and not bypassed:
            bypassed = True
            return []
        return real_validate(state, division_id, team_name, fields)
    monkeypatch.setattr(participant_import, "creation_issues", validation)
    response = request(context, "create", [row(context, "Before race"), row(context, "Race duplicate"), row(context, "After race")])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["created"]) == 2 and len(data["errors"]) == 1
    assert data["errors"][0]["row"] == 2 and data["errors"][0]["code"] == "duplicate_team_name"
    with store._session() as db:
        names = set(db.scalars(select(ParticipantTeamRow.team_name).where(ParticipantTeamRow.contest_id == context["cid"])))
    assert names == {"Race duplicate", "Before race", "After race"}


def test_preview_and_create_use_same_normalization_and_enforce_scope(context):
    rows = [row(context, "  Normalized  ", "  SameCase@EXAMPLE.COM  ")]
    preview = request(context, "preview", rows).json()["data"]["rows"][0]
    result = request(context, "create", rows).json()["data"]["results"][0]
    assert result["normalized"] == preview["normalized"]
    assert result["corrections"] == preview["corrections"]
    assert client.post(context["path"] + "preview", json={"teams": rows}).status_code == 401
    store.upsert_contest_operator(context["cid"], f"owner-{uuid4().hex}@example.com", "Owner", ["master"])
    viewer = store.upsert_contest_operator(context["cid"], f"viewer-{uuid4().hex}@example.com", "Viewer", ["submissions_viewer"])
    session = login(str(viewer.email))
    denied = {"Authorization": "Bearer " + session["operator_session"]["access_token"]}
    assert request(context, "preview", rows, denied).status_code == 403
    assert request(context, "create", rows, denied).status_code == 403


@pytest.mark.parametrize("failure", [ValueError("queue failed"), IntegrityError("INSERT", {}, Exception("queue failed"))])
def test_invitation_failure_keeps_committed_team_created_and_does_not_request_resubmission(context, monkeypatch, failure):
    def fail_queue(*args):
        raise failure
    monkeypatch.setattr(store, "enqueue_participant_invite_for_team", fail_queue)
    submitted = row(context, "Created despite invitation failure")
    response = request(context, "create", [submitted])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"] == {"total": 1, "created": 1, "failed": 0, "warnings": 1}
    assert data["errors"] == []
    result = data["results"][0]
    assert result["status"] == "created" and result["valid"] is True
    assert result["warnings"][0]["code"] == "invitation_failed"
    assert result["participant_team_id"] == data["created"][0]["participant_team_id"]
    with store._session() as db:
        assert db.get(ParticipantTeamRow, result["participant_team_id"]).team_name == submitted["team_name"]


def test_normalized_names_reject_remaining_control_characters_for_preview_and_create(context):
    rows = [row(context, "Team\nName"), row(context, "Leader control", leader={"name": "Person\x00Name", "email": "leader@example.com"}),
        row(context, "Member control", members=[{"name": "Person\x7fName", "email": "member@example.com"}]),
        row(context, "\t Trimmed edge controls \n", leader={"name": "\n Person \t", "email": "trimmed@example.com"})]
    checked = request(context, "preview", rows).json()["data"]
    assert checked["summary"]["ready"] == 1
    assert [item["errors"][0]["code"] for item in checked["rows"][:3]] == ["invalid_name"] * 3
    assert [item["errors"][0]["field"] for item in checked["rows"][:3]] == ["team_name", "leader.name", "members.0.name"]
    saved = request(context, "create", rows).json()["data"]
    assert len(saved["created"]) == 1 and len(saved["errors"]) == 3
    assert saved["created"][0]["team_name"] == "Trimmed edge controls"
    assert saved["created"][0]["members"][0]["name"] == "Person"


@pytest.mark.parametrize("action", ["preview", "create"])
def test_bulk_request_has_explicit_bounded_row_count(context, action):
    response = request(context, action, [None] * 1001)
    assert response.status_code == 422
    assert request(context, action, []).status_code == 200
