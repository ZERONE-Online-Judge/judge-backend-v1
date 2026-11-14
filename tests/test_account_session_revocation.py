import asyncio
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
from app.orm_models import StaffAccountRow
from app.services.session_events import resolve_session_watch, session_events, session_watch_active
from app.services.store import store

client = TestClient(app)


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def login(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.fixture
def context():
    email = f"revocation-{uuid4().hex}@zoj.com"
    other_email = f"other-{uuid4().hex}@zoj.com"
    contests = []
    for number in range(2):
        contest = store.create_contest(f"Revocation {number}", "Test", "Test", now_utc() - timedelta(minutes=10), status=ContestStatus.RUNNING)
        cid = contest.contest_id
        division = store.create_contest_division(cid, "all", "All")
        team = store.create_participant_team(cid, division.division_id, "Target", "Target", email, [])
        store.create_participant_team(cid, division.division_id, "Other", "Other", other_email, [])
        problem = store.create_problem(cid, division.division_id, "A", "A", "secret statement", 1000, 128, {}, 1)
        contests.append((cid, team, problem))
    general = login(email)
    other = login(other_email)
    participant_tokens = []
    for cid, _, _ in contests:
        issued = client.post(f"/api/auth/general/contests/{cid}/participant-session", headers=headers(general["access_token"]))
        assert issued.status_code == 200
        participant_tokens.append(issued.json()["data"]["access_token"])
    # A legacy second device session must be revoked as well.
    with store._session() as db:
        second_device = store._issue_general_session(db, email, store._general_profile(db, email))
    return {"email": email, "contests": contests, "general": general, "other": other, "tokens": participant_tokens, "second": second_device}


def replace_login(context):
    # The account owner can still confirm moving their login to a new device.
    return login(context["email"])


def test_confirmed_new_login_revokes_old_sessions_across_devices_and_contests(context):
    c = context
    for cid, _, _ in c["contests"]:
        assert client.get(f"/api/contests/{cid}/problems", headers=headers(c["general"]["access_token"])).status_code == 200
    replace_login(c)
    for session in (c["general"], c["second"]):
        assert client.get("/api/auth/general/me", headers=headers(session["access_token"])).status_code == 401
        assert client.post("/api/auth/general/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
        for cid, _, _ in c["contests"]:
            assert client.get(f"/api/contests/{cid}/problems", headers=headers(session["access_token"])).status_code == 404
            assert client.post(f"/api/auth/general/contests/{cid}/participant-session", headers=headers(session["access_token"])).status_code == 401
            assert store.issue_participant_session_for_general(c["email"], cid, session["access_token"]) is None
    for (cid, team, problem), token in zip(c["contests"], c["tokens"]):
        assert client.get(f"/api/contests/{cid}/participant-session/me", headers=headers(token)).status_code == 401
        assert client.post(f"/api/contests/{cid}/problems/{problem.problem_id}/submissions", headers=headers(token), json={"language": "cpp17", "source_code": "int main(){}"}).status_code == 401
        assert store.teams[team.participant_team_id].members[0].active_sessions == 0
    assert client.get("/api/auth/general/me", headers=headers(c["other"]["access_token"])).status_code == 200
    # Replacing a login is not an account ban: a verified login can reenter.
    fresh = login(c["email"])
    assert client.post(f"/api/auth/general/contests/{c['contests'][0][0]}/participant-session", headers=headers(fresh["access_token"])).status_code == 200


def test_session_watch_survives_token_refresh_and_reports_revocation(context):
    watch = resolve_session_watch(context["general"]["access_token"])
    assert watch and session_watch_active(watch)
    participant_watch = resolve_session_watch(context["tokens"][0])
    assert participant_watch and session_watch_active(participant_watch)
    refreshed = client.post("/api/auth/general/refresh", json={"refresh_token": context["general"]["refresh_token"]})
    assert refreshed.status_code == 200
    assert session_watch_active(watch)

    class Connected:
        async def is_disconnected(self):
            return False

    async def verify_events():
        events = session_events(Connected(), watch)
        assert "event: ready" in await anext(events)
        replace_login(context)
        assert "event: session_revoked" in await anext(events)
        with pytest.raises(StopAsyncIteration):
            await anext(events)
    asyncio.run(verify_events())
    assert not session_watch_active(participant_watch)
    assert resolve_session_watch(refreshed.json()["data"]["access_token"]) is None
    assert client.get("/api/auth/session-events").status_code == 401
    assert client.get("/api/auth/session-events", headers=headers("invalid")).status_code == 401


def test_confirmed_new_login_also_revokes_legacy_staff_session(context):
    other = store.create_contest("Staff", "Test", "Test")
    account = store.upsert_contest_operator(other.contest_id, context["email"], "Target", ["master"])
    with store._session() as db:
        row = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == account.email))
        legacy = store._issue_staff_session(db, row)
    watch = resolve_session_watch(legacy["access_token"])
    assert watch and session_watch_active(watch)
    replace_login(context)
    assert store.get_staff_by_access_token(legacy["access_token"]) is None
    assert store.refresh_staff_session(legacy["refresh_token"]) is None
    assert not session_watch_active(watch)
