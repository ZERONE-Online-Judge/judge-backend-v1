"""Session labels describe the session issuer, never the current viewer."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app.database import SessionLocal
from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import AccountPasswordRow, GeneralSessionRow, IdentityAccountRow, StaffSessionRow, TeamSessionRow
from app.services.client_environment import classify_user_agent, decode_environment, environment, SESSION_DESCRIPTION_LIMIT
from app.services.security import token_hash
from app.services.store import store
from app.settings import settings
from test_passkeys import PASSWORD, PREFIX, assertion, browser, enroll, login


CHROME_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SAFARI_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
EDGE_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 EdgA/131.0.0.0"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"


@pytest.mark.parametrize("ua,browser_name,os,device", [
    (CHROME_WINDOWS, "Chrome", "Windows", "desktop"),
    (SAFARI_IOS, "Safari", "iOS", "mobile"),
    (SAFARI_IOS.replace("Version/18.0", "CriOS/131.0.0.0"), "Chrome", "iOS", "mobile"),
    (SAFARI_IOS.replace("Version/18.0", "FxiOS/132.0"), "Firefox", "iOS", "mobile"),
    (SAFARI_IOS.replace("Version/18.0", "EdgiOS/131.0"), "Edge", "iOS", "mobile"),
    (EDGE_ANDROID, "Edge", "Android", "mobile"),
    (EDGE_ANDROID.replace(" EdgA/131.0.0.0", ""), "Chrome", "Android", "mobile"),
    (EDGE_ANDROID.replace(" Mobile", "").replace(" EdgA/131.0.0.0", ""), "Chrome", "Android", "tablet"),
    (SAFARI_IOS.replace("iPhone; CPU iPhone", "iPad; CPU"), "Safari", "iPadOS", "tablet"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15", "Safari", "macOS", "desktop"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/605.1.15", "Safari", "macOS", "unknown"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0", "Firefox", "Windows", "desktop"),
    (FIREFOX_LINUX, "Firefox", "Linux", "desktop"),
    ("Mozilla/5.0 (X11; CrOS x86_64 16033.0.0) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "Chrome", "ChromeOS", "desktop"),
    ("python-httpx/0.28.1", "Unknown", "Unknown", "unknown"),
    (None, "Unknown", "Unknown", "unknown"),
    ("unrecognized client", "Unknown", "Unknown", "unknown"),
])
def test_parser_identifies_specific_browser_before_compatible_safari_chrome_tokens(ua, browser_name, os, device):
    actual = classify_user_agent(ua)
    assert actual == environment(browser_name, os, device)
    assert "Pixel" not in actual["label"] and "131" not in actual["label"]


@pytest.mark.parametrize("value", [None, "broken JSON", "[]", '{"browser":[],"os":{},"device":[]}',
    '{"browser":"<script>","os":"Invented Device","device":"server","label":"unsafe"}'])
def test_unknown_or_malformed_legacy_environment_is_honestly_unknown(value):
    assert decode_environment(value) == environment()


def identity_email():
    email = f"environment-{uuid4().hex}@example.com"
    with SessionLocal() as db:
        db.add(IdentityAccountRow(email=email, display_name="Environment user"))
        db.commit()
    return email


def otp(client, email, ua, force=True):
    return client.post(PREFIX + "/otp/verify", headers={"User-Agent": ua}, json={
        "email": email, "otp_code": "", "force_new_session": force})


def persisted_general(token):
    with SessionLocal() as db:
        row = db.scalar(select(GeneralSessionRow).where(GeneralSessionRow.access_token_hash == token_hash(token)))
        return decode_environment(row.client_environment)


def test_general_otp_conflict_me_and_refresh_keep_original_environment_until_new_login():
    client = TestClient(app, base_url="https://zoj.kr")
    email = identity_email()
    original = otp(client, email, CHROME_WINDOWS).json()["data"]
    expected = environment("Chrome", "Windows", "desktop")
    assert original["environment"] == persisted_general(original["access_token"]) == expected
    conflict = otp(client, email, EDGE_ANDROID, force=False)
    assert conflict.status_code == 409
    details = conflict.json()["error"]["details"]
    assert details["active_session_count"] == 1 and details["sessions"][0]["environment"] == expected
    assert not any(key in json.dumps(details) for key in ["access_token", "refresh_token", "token_hash", "session_id"])
    checked = client.get(PREFIX + "/me", headers={"Authorization": "Bearer " + original["access_token"], "User-Agent": SAFARI_IOS})
    assert checked.status_code == 200 and checked.json()["data"]["environment"] == expected
    refreshed = client.post(PREFIX + "/refresh", json={"refresh_token": original["refresh_token"]}, headers={"User-Agent": FIREFOX_LINUX})
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["environment"] == persisted_general(refreshed.json()["data"]["access_token"]) == expected
    replaced = otp(client, email, SAFARI_IOS).json()["data"]
    assert replaced["environment"] == persisted_general(replaced["access_token"]) == environment("Safari", "iOS", "mobile")
    assert client.get(PREFIX + "/me", headers={"Authorization": "Bearer " + refreshed.json()["data"]["access_token"]}).status_code == 401


def test_password_and_real_passkey_transfer_capture_their_own_request_environment(browser):
    import time
    from app.services.account_passwords import hash_password
    browser.headers.update({"User-Agent": CHROME_WINDOWS})
    email, headers, _ = login(browser)
    auth = enroll(browser, headers)
    with SessionLocal() as db:
        db.add(AccountPasswordRow(email=email, password_hash=hash_password(PASSWORD), updated_at=int(time.time())))
        db.commit()
    password = browser.post(PREFIX + "/password/login", headers={"User-Agent": FIREFOX_LINUX}, json={
        "email": email, "password": PASSWORD, "force_new_session": True})
    assert password.status_code == 200, password.text
    session = password.json()["data"]
    assert session["environment"] == persisted_general(session["access_token"]) == environment("Firefox", "Linux", "desktop")
    direct = browser.post(PREFIX + "/passkeys/login/verify", headers={"User-Agent": EDGE_ANDROID},
        json={**assertion(browser, auth), "force_new_session": True})
    assert direct.status_code == 200, direct.text
    assert direct.json()["data"]["environment"] == environment("Edge", "Android", "mobile")
    conflict = browser.post(PREFIX + "/passkeys/login/verify", json=assertion(browser, auth, count=2))
    assert conflict.status_code == 409
    details = conflict.json()["error"]["details"]
    assert details["sessions"][0]["environment"] == environment("Edge", "Android", "mobile")
    transfer = browser.post(PREFIX + "/passkeys/login/transfer", headers={"User-Agent": SAFARI_IOS}, json={
        "email": email, "proof_token": details["transfer_token"]})
    assert transfer.status_code == 200, transfer.text
    assert transfer.json()["data"]["environment"] == persisted_general(transfer.json()["data"]["access_token"]) == environment("Safari", "iOS", "mobile")


def make_contest(email, suffix):
    contest = store.create_contest("Environment " + suffix, "ZOJ", "Test", now_utc() + timedelta(days=1), status=ContestStatus.DRAFT)
    division = store.create_contest_division(contest.contest_id, "A", "General")
    team = store.create_participant_team(contest.contest_id, division.division_id, "Team", "Leader", email, [])
    return contest, team


def master_headers(client):
    data = otp(client, "test3@zoj.com", FIREFOX_LINUX).json()["data"]
    return {"Authorization": "Bearer " + data["access_token"]}


def test_scoped_operator_sessions_do_not_include_other_contests_or_global_sessions():
    client = TestClient(app)
    email = identity_email()
    first, first_team = make_contest(email, "first")
    second, _ = make_contest(email, "second")
    general = otp(client, email, CHROME_WINDOWS).json()["data"]
    headers = {"Authorization": "Bearer " + general["access_token"]}
    tokens = []
    for contest, ua in [(first, SAFARI_IOS), (second, EDGE_ANDROID)]:
        response = client.post(PREFIX + f"/contests/{contest.contest_id}/participant-session", headers={**headers, "User-Agent": ua})
        assert response.status_code == 200, response.text
        tokens.append(response.json()["data"]["access_token"])
    owner = store.upsert_contest_operator(first.contest_id, f"operator-{uuid4().hex}@example.com", "Scoped operator", ["master"])
    operator_session = otp(TestClient(app), str(owner.email), FIREFOX_LINUX).json()["data"]
    operator_headers = {"Authorization": "Bearer " + operator_session["access_token"]}
    response = client.get(f"/api/operator/contests/{first.contest_id}/participants", headers=operator_headers)
    assert response.status_code == 200
    member = response.json()["data"][0]["members"][0]
    assert member["active_sessions"] == 1
    assert [item["environment"] for item in member["sessions"]] == [environment("Safari", "iOS", "mobile")]
    assert {item["contest_id"] for item in member["sessions"]} == {first.contest_id}
    for payload in [store.teams_by_ids([first_team.participant_team_id])[first_team.participant_team_id].model_dump(mode="json"),
        client.get(f"/api/contests/{first.contest_id}/participant-session/me", headers={"Authorization": "Bearer " + tokens[0]}).json()["data"]["team"]]:
        assert "sessions" not in payload["members"][0]
    assert client.get(f"/api/operator/contests/{second.contest_id}/participants", headers=operator_headers).status_code == 403
    assert client.get("/api/admin/users/detail", params={"email": email}, headers=operator_headers).status_code == 403
    detail = client.get("/api/admin/users/detail", params={"email": email}, headers=master_headers(TestClient(app)))
    assert detail.status_code == 200
    assert {session["scope"] for session in detail.json()["data"]["sessions"]} == {"general", "participant"}
    assert len(detail.json()["data"]["sessions"]) == 3
    assert client.get("/api/admin/users/detail", params={"email": email}, headers=headers).status_code == 401
    assert client.get(f"/api/operator/contests/{first.contest_id}/participants", headers=headers).status_code == 401


def test_legacy_participant_otp_and_internal_staff_issuance_store_only_explicit_environment():
    client = TestClient(app)
    email = identity_email()
    contest, _ = make_contest(email, "legacy")
    response = client.post(f"/api/contests/{contest.contest_id}/participant-login/otp/verify",
        headers={"User-Agent": SAFARI_IOS}, json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200
    with SessionLocal() as db:
        session = db.scalar(select(TeamSessionRow).where(TeamSessionRow.access_token_hash == token_hash(response.json()["data"]["access_token"])))
        assert decode_environment(session.client_environment) == environment("Safari", "iOS", "mobile")
    account = store.upsert_contest_operator(contest.contest_id, f"staff-{uuid4().hex}@example.com", "Owner", ["master"])
    code = store.create_staff_otp(str(account.email))
    staff = store.verify_staff_otp(str(account.email), code, client_environment=classify_user_agent(CHROME_WINDOWS))
    assert staff["environment"] == environment("Chrome", "Windows", "desktop")
    assert store.refresh_staff_session(staff["refresh_token"])["environment"] == staff["environment"]
    legacy = store.verify_staff_otp(str(account.email), code)
    assert legacy["environment"] == environment()


def test_authorized_logs_use_their_own_saved_user_agent_not_the_viewer():
    client = TestClient(app)
    access = store.append_access_log(event_type="general_login", account_scope="general", email="environment-log@example.com", user_agent=SAFARI_IOS)
    audit = store.append_operational_audit_log(scope="admin", action="Test", method="POST", path="/api/admin/test", status_code=200, user_agent=EDGE_ANDROID)
    headers = master_headers(client)
    access_rows = client.get("/api/admin/access-logs", params={"email": "environment-log@example.com"}, headers={**headers, "User-Agent": FIREFOX_LINUX})
    assert access_rows.status_code == 200
    assert next(item for item in access_rows.json()["data"] if item["access_log_id"] == access.access_log_id)["environment"] == environment("Safari", "iOS", "mobile")
    audit_rows = client.get("/api/admin/audit-logs", headers={**headers, "User-Agent": FIREFOX_LINUX})
    assert audit_rows.status_code == 200
    assert next(item for item in audit_rows.json()["data"] if item["operational_audit_log_id"] == audit.operational_audit_log_id)["environment"] == environment("Edge", "Android", "mobile")


def test_legacy_unknown_and_many_active_sessions_are_bounded_without_secret_fields():
    from app.services.client_environment import active_user_sessions
    email = identity_email()
    with SessionLocal() as db:
        for index in range(SESSION_DESCRIPTION_LIMIT + 3):
            db.add(GeneralSessionRow(email=email, access_token_hash=uuid4().hex, refresh_token_hash=uuid4().hex,
                issued_at=now_utc(), access_expires_at=now_utc() + timedelta(hours=1), refresh_expires_at=now_utc() + timedelta(days=1)))
        db.commit()
        sessions = active_user_sessions(db, email)
        assert len(sessions) == SESSION_DESCRIPTION_LIMIT
        assert all(item["environment"] == environment() for item in sessions)
        assert all(set(item) == {"scope", "issued_at", "last_seen_at", "expires_at", "environment"} for item in sessions)
        summary = store._active_login_session_summary(db, email)
        assert summary["active_session_count"] == SESSION_DESCRIPTION_LIMIT + 3
        assert len(summary["sessions"]) == SESSION_DESCRIPTION_LIMIT


def test_nullable_session_environment_migration_preserves_legacy_rows_and_downgrades(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'session-migration.db'}"
    migration_engine = create_engine(url)
    with migration_engine.begin() as db:
        for table in ("general_sessions", "staff_sessions", "team_sessions"):
            db.execute(text(f"CREATE TABLE {table} (legacy_id TEXT PRIMARY KEY)"))
            db.execute(text(f"INSERT INTO {table} (legacy_id) VALUES ('keep-me')"))
    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config("alembic.ini")
    command.stamp(cfg, "0061_resolver_release_default")
    command.upgrade(cfg, "head")
    for table in ("general_sessions", "staff_sessions", "team_sessions"):
        columns = {column["name"]: column for column in inspect(migration_engine).get_columns(table)}
        assert columns["client_environment"]["nullable"] is True
        with migration_engine.connect() as db:
            assert db.execute(text(f"SELECT client_environment FROM {table} WHERE legacy_id='keep-me'")).scalar() is None
    command.downgrade(cfg, "0061_resolver_release_default")
    assert "client_environment" not in {column["name"] for column in inspect(migration_engine).get_columns("general_sessions")}
    migration_engine.dispose()
