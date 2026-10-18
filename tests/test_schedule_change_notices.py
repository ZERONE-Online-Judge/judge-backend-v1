import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus
from app.services.store import store
from app.settings import settings


@pytest.fixture
def schedule_client(monkeypatch):
    monkeypatch.setattr(settings, "feature_emergency_notice_auto", True)
    client = TestClient(app)
    login = client.post("/api/auth/general/otp/verify", json={
        "email": "test3@zoj.com", "otp_code": "", "force_new_session": True,
    })
    assert login.status_code == 200, login.text
    token = login.json()["data"]["operator_session"]["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def create_contest(status):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    contest = store.create_contest(
        "일정 변경 테스트 " + uuid4().hex[:8], "테스트", "",
        start_at=now - timedelta(hours=1) if status == ContestStatus.RUNNING else now + timedelta(days=1),
        freeze_at=now + timedelta(days=1, hours=1),
        end_at=now + timedelta(days=1, hours=2),
        status=status,
    )
    store.update_contest_settings(contest.contest_id, emergency_notice="기존 운영 안내")
    return contest


@pytest.mark.parametrize("status", list(ContestStatus))
def test_schedule_change_only_announces_open_or_running_contests(schedule_client, status):
    contest = create_contest(status)
    new_end = contest.end_at + timedelta(minutes=10)
    response = schedule_client.patch(f"/api/operator/contests/{contest.contest_id}/settings", json={
        "end_at": new_end.isoformat(),
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["end_at"] == new_end.isoformat().replace("+00:00", "Z")
    saved = store.contests[contest.contest_id]
    notices = store.contest_notices_for_view(contest.contest_id, operator=True)
    if status in {ContestStatus.OPEN, ContestStatus.RUNNING}:
        assert [notice.title for notice in notices] == ["대회 일정 변경"]
        assert "종료 시간이" in saved.emergency_notice
        assert response.json()["data"]["emergency_notice"] == saved.emergency_notice
    else:
        assert notices == []
        assert saved.emergency_notice == response.json()["data"]["emergency_notice"] == "기존 운영 안내"


@pytest.mark.parametrize("next_status", [ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD,
    ContestStatus.SCHEDULED, ContestStatus.ENDED, ContestStatus.FINALIZED, ContestStatus.ARCHIVED])
def test_hiding_or_ending_contest_with_schedule_edit_does_not_announce(schedule_client, next_status):
    contest = create_contest(ContestStatus.OPEN)
    response = schedule_client.patch(f"/api/operator/contests/{contest.contest_id}/settings", json={
        "status": next_status.value,
        "end_at": (contest.end_at + timedelta(minutes=10)).isoformat(),
        "emergency_notice": "직접 작성한 안내",
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == next_status.value
    assert store.contest_notices_for_view(contest.contest_id, operator=True) == []
    assert store.contests[contest.contest_id].emergency_notice == "직접 작성한 안내"


def test_schedule_edit_that_ends_contest_does_not_announce(schedule_client):
    contest = create_contest(ContestStatus.RUNNING)
    new_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    response = schedule_client.patch(f"/api/operator/contests/{contest.contest_id}/settings", json={
        "end_at": new_end.isoformat(),
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == ContestStatus.ENDED.value
    assert store.contest_notices_for_view(contest.contest_id, operator=True) == []
    assert store.contests[contest.contest_id].emergency_notice == "기존 운영 안내"


def test_disabling_automatic_notices_preserves_manual_banner(schedule_client, monkeypatch):
    contest = create_contest(ContestStatus.OPEN)
    monkeypatch.setattr(settings, "feature_emergency_notice_auto", False)
    response = schedule_client.patch(f"/api/operator/contests/{contest.contest_id}/settings", json={
        "end_at": (contest.end_at + timedelta(minutes=10)).isoformat(),
    })
    assert response.status_code == 200, response.text
    assert store.contest_notices_for_view(contest.contest_id, operator=True) == []
    assert store.contests[contest.contest_id].emergency_notice == "기존 운영 안내"
