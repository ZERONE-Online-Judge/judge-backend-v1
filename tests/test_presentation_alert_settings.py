"""Per-contest presentation reminders never change fixed countdown settings."""
import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import ContestRow
from app.presentation_alerts import PRESENTATION_ALERT_FIELDS
from app.routers.admin import ContestCreateRequest
from app.routers.operator import ContestSettingsUpdateRequest
from app.services.errors import AppError
from app.services.scoreboard_events import reveal_versions
from app.services.store import store

DEFAULTS = [30, 20, 10, 5]


def login(email):
    with TestClient(app) as client:
        response = client.post("/api/auth/general/otp/verify", json={
            "email": email, "otp_code": "", "force_new_session": True,
        })
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["operator_session"]["access_token"]}


@pytest.fixture
def context():
    contest = store.create_contest("알림 시각 시험", "ZOJ", "Synthetic contest",
        start_at=now_utc() + timedelta(days=1), status=ContestStatus.OPEN)
    cid = contest.contest_id
    store.create_contest_division(cid, "all", "전체")
    email = f"alerts-owner-{uuid4().hex}@example.com"
    store.upsert_contest_operator(cid, email, "운영자", ["master"])
    with TestClient(app) as client:
        yield {"client": client, "contest": contest, "cid": cid, "headers": login(email),
            "path": f"/api/operator/contests/{cid}/settings"}


def test_new_contests_and_model_defaults_are_independent_lists(context):
    contest = context["contest"]
    for field in PRESENTATION_ALERT_FIELDS:
        assert getattr(contest, field) == DEFAULTS
    copy = contest.model_dump(exclude=set(PRESENTATION_ALERT_FIELDS))
    first = type(contest).model_validate(copy)
    second = type(contest).model_validate(copy)
    first.presentation_freeze_alert_minutes.append(1)
    assert second.presentation_freeze_alert_minutes == DEFAULTS
    assert first.presentation_end_alert_minutes == DEFAULTS
    with store._session() as db:
        row = db.get(ContestRow, contest.contest_id)
        assert row.presentation_freeze_alert_minutes == DEFAULTS
        assert row.presentation_end_alert_minutes == DEFAULTS


def test_custom_and_disabled_minutes_persist_and_reach_both_presentation_payloads(context):
    c = context
    before = c["contest"]
    changed = c["client"].patch(c["path"], headers=c["headers"], json={
        "presentation_freeze_alert_minutes": [5, 45, 5, 1, 10080],
        "presentation_end_alert_minutes": [],
    })
    assert changed.status_code == 200, changed.text
    expected = {"presentation_freeze_alert_minutes": [10080, 45, 5, 1], "presentation_end_alert_minutes": []}
    for field, value in expected.items():
        assert changed.json()["data"][field] == value
        assert getattr(store.get_contest(c["cid"]), field) == value
    with store._session() as db:
        row = db.get(ContestRow, c["cid"])
        assert row.presentation_freeze_alert_minutes == expected["presentation_freeze_alert_minutes"]
        assert row.presentation_end_alert_minutes == []
    dashboard = c["client"].get(f"/api/operator/contests/{c['cid']}/dashboard", headers=c["headers"])
    operator_board = c["client"].get(f"/api/operator/contests/{c['cid']}/scoreboard/presentation", headers=c["headers"])
    account = c["client"].put(f"/api/operator/contests/{c['cid']}/scoreboard/presentation-account", headers=c["headers"])
    assert dashboard.status_code == operator_board.status_code == account.status_code == 200
    display_login = c["client"].post("/api/auth/presentation/login", json={"email": account.json()["data"]["email"]},
        headers={"x-real-ip": str(uuid4())})
    assert display_login.status_code == 200, display_login.text
    display = c["client"].get(f"/api/presentation/contests/{c['cid']}/scoreboard", headers={
        "Authorization": "Bearer " + display_login.json()["data"]["access_token"],
    })
    assert display.status_code == 200, display.text
    for response in (dashboard, operator_board, display):
        data = response.json()["data"]["contest"]
        assert all(data[field] == value for field, value in expected.items())
        assert all(data[field] == getattr(before, field).isoformat().replace("+00:00", "Z")
            for field in ("start_at", "freeze_at", "end_at"))
        assert not any("countdown" in key for key in data)
    assert display.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("field", PRESENTATION_ALERT_FIELDS)
@pytest.mark.parametrize("invalid", [None, True, 5, "5,10", [True], [False], [1.0], ["5"], [0], [-1], [10081], [1] * 33])
def test_invalid_alert_minutes_are_rejected_without_partial_settings_write(context, field, invalid):
    c = context
    response = c["client"].patch(c["path"], headers=c["headers"], json={field: invalid, "title": "Must not persist"})
    assert response.status_code == 422, response.text
    saved = store.get_contest(c["cid"])
    assert saved.title == c["contest"].title
    assert all(getattr(saved, key) == DEFAULTS for key in PRESENTATION_ALERT_FIELDS)


def test_empty_lists_disable_independently_and_omitted_field_is_unchanged(context):
    c = context
    for body in ({"presentation_freeze_alert_minutes": []}, {"presentation_end_alert_minutes": [2, 60, 2]}, {"overview": "Changed overview"}):
        result = c["client"].patch(c["path"], headers=c["headers"], json=body)
        assert result.status_code == 200, result.text
    saved = store.get_contest(c["cid"])
    assert saved.presentation_freeze_alert_minutes == []
    assert saved.presentation_end_alert_minutes == [60, 2]
    assert saved.overview == "Changed overview"


@pytest.mark.parametrize("role, expected_status", [("settings_manager", 200), ("scoreboard_manager", 403), ("notices_manager", 403), ("audit_viewer", 403)])
def test_warning_preferences_use_settings_permission(context, role, expected_status):
    c = context
    email = f"alerts-{role}-{uuid4().hex}@example.com"
    store.upsert_contest_operator(c["cid"], email, "권한 시험", [role])
    response = c["client"].patch(c["path"], headers=login(email), json={"presentation_end_alert_minutes": [3]})
    assert response.status_code == expected_status, response.text
    assert store.get_contest(c["cid"]).presentation_end_alert_minutes == ([3] if expected_status == 200 else DEFAULTS)


def test_unauthenticated_and_cross_contest_operators_cannot_update_alerts(context):
    c = context
    with TestClient(app) as anonymous:
        assert anonymous.patch(c["path"], json={"presentation_end_alert_minutes": []}).status_code == 401
    other = store.create_contest("별도 대회", "ZOJ", "", status=ContestStatus.OPEN)
    email = f"alerts-outsider-{uuid4().hex}@example.com"
    store.upsert_contest_operator(other.contest_id, email, "다른 운영자", ["master"])
    response = c["client"].patch(c["path"], headers=login(email), json={"presentation_end_alert_minutes": []})
    assert response.status_code == 403
    assert store.get_contest(c["cid"]).presentation_end_alert_minutes == DEFAULTS


def test_alert_update_changes_display_refresh_signal_only_when_effective_value_changes(context):
    cid = context["cid"]
    initial = reveal_versions({cid})
    store.update_contest_settings(cid, presentation_freeze_alert_minutes=[30, 5])
    freeze_changed = reveal_versions({cid})
    assert freeze_changed != initial
    store.update_contest_settings(cid, presentation_freeze_alert_minutes=[5, 30, 5])
    assert reveal_versions({cid}) == freeze_changed
    store.update_contest_settings(cid, presentation_end_alert_minutes=[])
    assert reveal_versions({cid}) != freeze_changed


def test_internal_store_enforces_same_strict_validation(context):
    cid = context["cid"]
    for invalid in (None, [True], [1.0], ["5"], [0], [1] * 33):
        with pytest.raises(AppError) as error:
            store.update_contest_settings(cid, title="Must not persist", presentation_end_alert_minutes=invalid)
        assert error.value.status_code == 422
    assert store.get_contest(cid).title == context["contest"].title


def test_invalid_second_category_rolls_back_first_and_ended_contests_can_edit_preferences(context):
    c = context
    response = c["client"].patch(c["path"], headers=c["headers"], json={
        "presentation_freeze_alert_minutes": [], "presentation_end_alert_minutes": [False],
    })
    assert response.status_code == 422
    assert store.get_contest(c["cid"]).presentation_freeze_alert_minutes == DEFAULTS
    store.update_contest_settings(c["cid"], status=ContestStatus.ENDED)
    response = c["client"].patch(c["path"], headers=c["headers"], json={"presentation_end_alert_minutes": [2]})
    assert response.status_code == 200, response.text
    assert store.get_contest(c["cid"]).presentation_end_alert_minutes == [2]


def test_running_contest_can_update_preferences_without_operation_lock_or_change_notice(context, monkeypatch):
    c = context
    now = now_utc()
    before = store.update_contest_settings(c["cid"], status=ContestStatus.RUNNING,
        start_at=now - timedelta(hours=1), freeze_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2))

    async def unexpected_notice(*args, **kwargs):
        pytest.fail("Warning preferences must not request a schedule-change notice")

    monkeypatch.setattr("app.routers.operator.confirm_change", unexpected_notice)
    response = c["client"].patch(c["path"], headers=c["headers"], json={
        "presentation_freeze_alert_minutes": [], "presentation_end_alert_minutes": [45, 2],
    })
    assert response.status_code == 200, response.text
    saved = store.get_contest(c["cid"])
    assert saved.status == ContestStatus.RUNNING
    assert saved.presentation_freeze_alert_minutes == [] and saved.presentation_end_alert_minutes == [45, 2]
    assert (saved.start_at, saved.freeze_at, saved.end_at) == (before.start_at, before.freeze_at, before.end_at)
    assert saved.emergency_notice == before.emergency_notice
    assert not [notice for notice in store.contest_notices.values() if notice.contest_id == c["cid"]]


def test_creation_accepts_custom_normalized_preferences_and_rejects_null():
    payload = ContestCreateRequest(organization_name="ZOJ", presentation_freeze_alert_minutes=[],
        presentation_end_alert_minutes=[1, 10080, 1])
    contest = store.create_contest("Custom creation", "ZOJ", "",
        presentation_freeze_alert_minutes=payload.presentation_freeze_alert_minutes,
        presentation_end_alert_minutes=payload.presentation_end_alert_minutes)
    assert contest.presentation_freeze_alert_minutes == []
    assert contest.presentation_end_alert_minutes == [10080, 1]
    with pytest.raises(ValidationError):
        ContestCreateRequest(organization_name="ZOJ", presentation_end_alert_minutes=None)
    assert not any("countdown" in key for key in ContestSettingsUpdateRequest.model_json_schema()["properties"])
    # Optional PATCH fields are arrays when provided, never nullable. Defaults
    # must not reset a category omitted from a partial settings request.
    request = ContestSettingsUpdateRequest(overview="Partial update")
    assert request.model_dump(exclude_unset=True) == {"overview": "Partial update"}
    for field in PRESENTATION_ALERT_FIELDS:
        assert ContestSettingsUpdateRequest.model_json_schema()["properties"][field]["type"] == "array"


def test_legacy_sqlite_schema_backfills_defaults_without_overwriting_disabled_array(tmp_path):
    database = tmp_path / "legacy.db"
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + str(database), "ENABLE_DEMO_SEED": "false",
        "LOCAL_OBJECT_STORAGE_ROOT": str(tmp_path / "objects")}
    setup = """
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import create_schema, engine, SessionLocal
from app.orm_models import ContestRow
create_schema()
with SessionLocal() as db:
    now=datetime.now(timezone.utc)
    db.add(ContestRow(contest_id='legacy',title='Keep title',organization_name='ZOJ',overview='',status='ended',start_at=now,end_at=now,freeze_at=now))
    db.commit()
with engine.begin() as db:
    db.execute(text("ALTER TABLE contests DROP COLUMN presentation_freeze_alert_minutes"))
    db.execute(text("ALTER TABLE contests DROP COLUMN presentation_end_alert_minutes"))
engine.dispose()
"""
    verify = """
import json
from sqlalchemy import text
from app.database import create_schema, engine
create_schema()
with engine.begin() as db:
    row=db.execute(text("SELECT presentation_freeze_alert_minutes,presentation_end_alert_minutes,title FROM contests WHERE contest_id='legacy'")).one()
    assert [json.loads(row[0]),json.loads(row[1]),row[2]] == [[30,20,10,5],[30,20,10,5],'Keep title']
    db.execute(text("UPDATE contests SET presentation_end_alert_minutes='[]' WHERE contest_id='legacy'"))
create_schema()
with engine.begin() as db:
    assert db.scalar(text("SELECT presentation_end_alert_minutes FROM contests WHERE contest_id='legacy'")) == '[]'
engine.dispose()
"""
    for code in (setup, verify):
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
