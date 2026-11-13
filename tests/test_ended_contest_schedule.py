"""Ended schedules are immutable, including requests that change the status."""
import os
from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import ContestRow
from app.services.errors import AppError
from app.services.store import store


@pytest.fixture
def context():
    clients = []

    def create(status=ContestStatus.ENDED, *, past=False):
        now = now_utc()
        start = now - timedelta(hours=4) if past else now + timedelta(days=1)
        contest = store.create_contest(
            "일정 잠금 회귀", "ZOJ", "Original description", start_at=start,
            freeze_at=start + timedelta(hours=2), end_at=start + timedelta(hours=3), status=status,
        )
        email = f"schedule-owner-{uuid4().hex}@example.com"
        store.upsert_contest_operator(contest.contest_id, email, "운영자", ["master"])
        client = TestClient(app)
        clients.append(client)
        login = client.post("/api/auth/general/otp/verify", json={
            "email": email, "otp_code": "", "force_new_session": True,
        })
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["data"]["operator_session"]["access_token"]}
        return client, contest, headers, f"/api/operator/contests/{contest.contest_id}/settings"

    yield create
    for client in clients:
        client.close()


def schedule(contest):
    return {field: getattr(contest, field) for field in ("start_at", "freeze_at", "end_at")}


@pytest.mark.parametrize("status", [ContestStatus.ENDED, ContestStatus.FINALIZED, ContestStatus.ARCHIVED])
@pytest.mark.parametrize("field", ["start_at", "freeze_at", "end_at"])
def test_explicitly_ended_status_rejects_each_schedule_change(context, status, field):
    client, contest, headers, path = context(status)
    response = client.patch(path, headers=headers, json={
        field: (getattr(contest, field) + timedelta(minutes=1)).isoformat(), "title": "Must not persist",
    })
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "contest_schedule_locked"
    assert "종료된 대회" in error["message"] and "다른 설정" in error["message"]
    assert error["details"]["fields"] == [field]
    saved = store.get_contest(contest.contest_id)
    assert schedule(saved) == schedule(contest)
    assert saved.title == contest.title


@pytest.mark.parametrize("status", [ContestStatus.SCHEDULED, ContestStatus.OPEN, ContestStatus.RUNNING])
def test_elapsed_end_time_is_locked_even_before_status_refresh(context, status):
    client, contest, headers, path = context(status, past=True)
    # Test the store first, before a GET has normalized the stored status.
    with pytest.raises(AppError) as error:
        store.update_contest_settings(contest.contest_id, end_at=now_utc() + timedelta(hours=1))
    assert error.value.code == "contest_schedule_locked"
    response = client.patch(path, headers=headers, json={
        "end_at": (now_utc() + timedelta(hours=1)).isoformat(),
    })
    assert response.status_code == 409
    assert schedule(store.get_contest(contest.contest_id)) == schedule(contest)


@pytest.mark.parametrize("status", [ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD])
def test_unscheduled_contest_with_past_placeholder_dates_is_not_ended(context, status):
    client, contest, headers, path = context(status, past=True)
    new_start = now_utc() + timedelta(days=2)
    response = client.patch(path, headers=headers, json={
        "start_at": new_start.isoformat(),
        "freeze_at": (new_start + timedelta(hours=2)).isoformat(),
        "end_at": (new_start + timedelta(hours=3)).isoformat(),
    })
    assert response.status_code == 200, response.text
    assert store.get_contest(contest.contest_id).start_at == new_start


def test_same_instants_and_other_settings_remain_editable_after_end(context):
    client, contest, headers, path = context(past=True)
    kst = timezone(timedelta(hours=9))
    response = client.patch(path, headers=headers, json={
        **{field: value.astimezone(kst).isoformat() for field, value in schedule(contest).items()},
        "title": "종료 후 제목 수정", "overview": "Updated description",
        "scoreboard_access_after_end": "public",
    })
    assert response.status_code == 200, response.text
    saved = store.get_contest(contest.contest_id)
    assert schedule(saved) == schedule(contest)
    assert saved.title == "종료 후 제목 수정"
    assert saved.overview == "Updated description"
    assert saved.scoreboard_access_after_end.value == "public"
    # Policy-only updates do not require clients to resend the schedule.
    response = client.patch(path, headers=headers, json={"scoreboard_access_after_end": "private"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("next_status", [ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD, ContestStatus.OPEN, ContestStatus.RUNNING])
def test_status_change_in_same_request_cannot_bypass_saved_end_state(context, next_status):
    client, contest, headers, path = context(past=True)
    response = client.patch(path, headers=headers, json={
        "status": next_status.value, "title": "Must not persist",
        "end_at": (now_utc() + timedelta(days=2)).isoformat(),
    })
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "contest_schedule_locked"
    saved = store.get_contest(contest.contest_id)
    assert saved.status == ContestStatus.ENDED
    assert saved.title == contest.title
    assert schedule(saved) == schedule(contest)


def test_store_checks_saved_row_before_any_partial_setting_mutation(context):
    _, contest, _, _ = context(past=True)
    with pytest.raises(AppError) as error:
        store.update_contest_settings(
            contest.contest_id, title="Must not persist", status=ContestStatus.OPEN,
            scoreboard_freeze_mode="frozen", end_at=now_utc() + timedelta(days=2),
        )
    assert error.value.code == "contest_schedule_locked"
    saved = store.get_contest(contest.contest_id)
    assert saved.title == contest.title
    assert saved.status == contest.status
    assert saved.scoreboard_freeze_mode == contest.scoreboard_freeze_mode
    with store._session() as db:
        assert db.get(ContestRow, contest.contest_id).scoreboard_frozen_at is None
    assert schedule(saved) == schedule(contest)


def test_store_rechecks_if_contest_ends_after_route_precheck(context, monkeypatch):
    client, contest, headers, path = context(ContestStatus.DRAFT)
    original_update = store.update_contest_settings

    def end_before_update(contest_id, **values):
        with store._session() as db:
            row = db.get(ContestRow, contest_id)
            row.status = ContestStatus.ENDED.value
            db.commit()
        return original_update(contest_id, **values)

    monkeypatch.setattr(store, "update_contest_settings", end_before_update)
    response = client.patch(path, headers=headers, json={
        "title": "Must not persist", "freeze_at": (contest.freeze_at + timedelta(minutes=1)).isoformat(),
    })
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "contest_schedule_locked"
    saved = store.get_contest(contest.contest_id)
    assert saved.title == contest.title and schedule(saved) == schedule(contest)


def test_ended_schedule_rejection_precedes_notice_preparation(context, monkeypatch):
    client, contest, headers, path = context(past=True)

    async def unexpected_notice(*args, **kwargs):
        pytest.fail("A rejected schedule must not prepare a change notice")

    monkeypatch.setattr("app.routers.operator.confirm_change", unexpected_notice)
    response = client.patch(path, headers=headers, json={
        "status": "open", "start_at": (now_utc() + timedelta(days=2)).isoformat(),
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "contest_schedule_locked"
    assert store.get_contest(contest.contest_id).emergency_notice == contest.emergency_notice


def test_authentication_and_settings_permission_still_precede_schedule_error(context):
    client, contest, _, path = context()
    payload = {"end_at": (contest.end_at + timedelta(minutes=1)).isoformat()}
    # The fixture client has general cookies, so use an independent anonymous client.
    with TestClient(app) as anonymous:
        assert anonymous.patch(path, json=payload).status_code == 401
    viewer_email = f"schedule-viewer-{uuid4().hex}@example.com"
    store.upsert_contest_operator(contest.contest_id, viewer_email, "조회 운영자", ["audit_viewer"])
    login = client.post("/api/auth/general/otp/verify", json={
        "email": viewer_email, "otp_code": "", "force_new_session": True,
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": "Bearer " + login.json()["data"]["operator_session"]["access_token"]}
    response = client.patch(path, headers=headers, json=payload)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_exact_end_boundary_is_already_locked(context, monkeypatch):
    _, contest, _, _ = context(ContestStatus.OPEN)
    monkeypatch.setattr("app.services.contest_visibility.now_utc", lambda: contest.end_at)
    with pytest.raises(AppError) as error:
        store.update_contest_settings(contest.contest_id, freeze_at=contest.freeze_at + timedelta(minutes=1))
    assert error.value.code == "contest_schedule_locked"


def test_other_settings_do_not_autocorrect_a_legacy_ended_schedule(context):
    client, contest, headers, path = context(past=True)
    # Older imported records can have a freeze outside the original window.
    with store._session() as db:
        row = db.get(ContestRow, contest.contest_id)
        row.freeze_at = contest.end_at + timedelta(hours=1)
        db.commit()
    before = store.get_contest(contest.contest_id)
    response = client.patch(path, headers=headers, json={"title": "제목만 수정"})
    assert response.status_code == 200, response.text
    saved = store.get_contest(contest.contest_id)
    assert saved.title == "제목만 수정" and schedule(saved) == schedule(before)
