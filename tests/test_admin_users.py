"""The user directory merges identities without exposing authentication data."""
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
from app.orm_models import AccountPasswordRow, ContestRow, IdentityAccountRow, SubmissionRow, TeamMemberRow
from app.services.store import store

client = TestClient(app)


def login(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={
        "email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


@pytest.fixture
def directory():
    marker = uuid4().hex
    email = f"directory-{marker}@example.com"
    memberships = []
    for number in [1, 2]:
        contest = store.create_contest(f"Directory {number}", "ZOJ", "", now_utc() + timedelta(days=number), status=ContestStatus.DRAFT)
        division = store.create_contest_division(contest.contest_id, "A", "일반부")
        team = store.create_participant_team(contest.contest_id, division.division_id, f"Team {number}", f"등록이름{number}-{marker}", email, [])
        memberships.append((contest, division, team))
    with store._session() as db:
        db.add(IdentityAccountRow(email=email, display_name="기본이름"))
        db.add(AccountPasswordRow(email=email, password_hash="NEVER_EXPOSE_THIS_HASH", updated_at=1790000000))
        db.commit()
    master = next(account for account in store.staff_accounts.values() if account.is_service_master)
    return dict(email=email, marker=marker, memberships=memberships, headers=login(str(master.email)))


def listing(c, **params):
    return client.get("/api/admin/users", headers=c["headers"], params={"q": c["marker"], **params})


def detail(c, email=None):
    return client.get("/api/admin/users/detail", headers=c["headers"], params={"email": email or c["email"]})


def test_merges_email_and_preserves_each_contest_name_and_password_status(directory):
    c = directory
    response = listing(c)
    assert response.status_code == 200, response.text
    assert response.json()["page"]["total_count"] == 1
    user = response.json()["data"][0]
    assert user["email"] == c["email"] and user["display_name"] == "기본이름"
    assert user["has_password"] is True and user["password_updated_at"]
    assert user["participant_contest_count"] == 2
    assert set(user["names"]) == {"기본이름", f"등록이름1-{c['marker']}", f"등록이름2-{c['marker']}"}
    found = detail(c, c["email"].upper())
    assert found.status_code == 200
    history = found.json()["data"]["participations"]
    assert [item["registered_name"] for item in history] == [f"등록이름2-{c['marker']}", f"등록이름1-{c['marker']}"]
    assert all(item["last_login_at"] is None for item in history)
    assert all(item["division_name"] == "일반부" for item in history)
    for payload in [response.text, found.text]:
        assert "NEVER_EXPOSE_THIS_HASH" not in payload
        assert "password_hash" not in payload and "access_token" not in payload


def test_searches_alternate_names_and_filters_passwords(directory):
    c = directory
    assert listing(c, q=f"등록이름2-{c['marker']}").json()["page"]["total_count"] == 1
    assert listing(c, password="set").json()["page"]["total_count"] == 1
    assert listing(c, password="unset").json()["data"] == []
    assert listing(c, q="%" + c["marker"]).json()["data"] == []
    assert listing(c, q=c["email"].upper()).json()["page"]["total_count"] == 1


def test_includes_unlogged_participants_and_identity_only_users_and_paginates(directory):
    c = directory
    with store._session() as db:
        db.add(IdentityAccountRow(email=f"aaa-{c['marker']}@example.com", display_name="계정만"))
        db.commit()
    # A participant who has never created a general login identity is included.
    contest, division, _ = c["memberships"][0]
    invite = f"zzz-{c['marker']}@example.com"
    store.create_participant_team(contest.contest_id, division.division_id, "Unlogged", "초대사용자", invite, [])
    first = listing(c, limit=1).json()
    second = listing(c, limit=1, offset=int(first["page"]["next_cursor"])).json()
    third = listing(c, limit=1, offset=2).json()
    assert first["page"]["total_count"] == 3
    assert len({page["data"][0]["email"] for page in [first, second, third]}) == 3
    assert third["page"]["next_cursor"] is None
    pending = detail(c, invite).json()["data"]
    assert pending["account_created_at"] is None and pending["has_password"] is False


def test_excludes_deleted_contests_and_internal_test_identities(directory):
    c = directory
    contest, division, _ = c["memberships"][0]
    internal = f"operator-test+{c['marker']}@local.zoj"
    store.create_participant_team(contest.contest_id, division.division_id, "__operator_test__hidden", "테스트", internal, [])
    test_only = f"hidden-{c['marker']}@example.com"
    store.create_participant_team(contest.contest_id, division.division_id, "__operator_test__normal_email", "테스트", test_only, [])
    assert detail(c, test_only).status_code == 404
    with store._session() as db:
        db.get(ContestRow, contest.contest_id).deleted_at = now_utc()
        db.commit()
    assert detail(c).json()["data"]["participant_contest_count"] == 1
    assert len(detail(c).json()["data"]["participations"]) == 1
    assert detail(c, internal).status_code == 404


def test_normalizes_case_across_sources_and_does_not_treat_master_access_as_participation(directory):
    c = directory
    member_id = c["memberships"][0][2].members[0].team_member_id
    with store._session() as db:
        db.get(TeamMemberRow, member_id).email = c["email"].upper()
        db.commit()
    assert listing(c).json()["page"]["total_count"] == 1
    assert len(detail(c).json()["data"]["participations"]) == 2
    master = next(account for account in store.staff_accounts.values() if account.is_service_master)
    result = detail(c, str(master.email)).json()["data"]
    assert result["operator_contests"] == []


def test_counts_only_personal_real_submissions_and_separates_operating_roles(directory):
    c = directory
    contest, division, team = c["memberships"][0]
    problem = store.create_problem(contest.contest_id, division.division_id, "A", "A", "", 1000, 128, {}, 1)
    with store._session() as db:
        member = db.get(TeamMemberRow, team.members[0].team_member_id)
        member.last_login_at = now_utc()
        for kind, status in [("participant", "accepted"), ("participant", "wrong_answer"), ("operator_test", "accepted")]:
            db.add(SubmissionRow(contest_id=contest.contest_id, division_id=division.division_id,
                problem_id=problem.problem_id, participant_team_id=team.participant_team_id,
                team_member_id=member.team_member_id, language="cpp17", source_code="", status=status, submission_kind=kind))
        db.commit()
    other_contest = c["memberships"][1][0]
    # Assign an actual operator without inventing participation from master access.
    operator = store.upsert_contest_operator(other_contest.contest_id, f"operator-{c['marker']}@example.com", "운영이름", ["master"])
    result = detail(c).json()["data"]
    row = next(item for item in result["participations"] if item["contest_id"] == contest.contest_id)
    assert (row["submission_count"], row["accepted_submission_count"]) == (2, 1)
    assert row["last_login_at"] and result["operator_contests"] == []
    staff = detail(c, str(operator.email)).json()["data"]
    assert staff["participations"] == []
    assert staff["operator_contests"][0]["role_title"] == "총괄"


def test_directory_is_service_master_only_and_rejects_invalid_filters(directory):
    c = directory
    ordinary = login(c["email"])
    operator = store.upsert_contest_operator(c["memberships"][0][0].contest_id, f"staff-{c['marker']}@example.com", "운영자", ["master"])
    for path in ["/api/admin/users", "/api/admin/users/detail?email=" + c["email"]]:
        assert TestClient(app).get(path).status_code == 401
        assert client.get(path, headers=ordinary).status_code == 401
        assert client.get(path, headers=login(str(operator.email))).status_code == 403
    for params in [{"limit": 101}, {"offset": -1}, {"password": "invalid"}]:
        assert listing(c, **params).status_code == 422
    assert detail(c, "unknown@example.com").status_code == 404
