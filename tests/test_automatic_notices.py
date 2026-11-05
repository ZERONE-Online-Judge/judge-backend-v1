import os
from datetime import datetime, timedelta, timezone
from importlib import import_module
from uuid import uuid4

import pytest

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.models import ContestStatus
from app.orm_models import ContestNoticeRow
from app.services.automatic_notices import (
    KST,
    LEGACY_SCHEDULE_FOOTER,
    compact_automatic_notice,
    legacy_scheduled_notice,
    scheduled_notice_copy,
    time_update_notice_body,
)
from app.services.notice_countdown import countdown_template
from app.services.store import store
from app.settings import settings


def at(day=5, hour=21, minute=4, year=2026):
    return datetime(year, 9, day, hour, minute, tzinfo=KST)


def test_time_change_mentions_only_changed_minutes():
    body = time_update_notice_body("2026 ZOAC", [
        ("start_at", at(hour=18, minute=4).replace(second=23), at(hour=18, minute=4)),
        ("freeze_at", at(), at(minute=30)),
        ("end_at", at(hour=22), at(hour=22)),
    ])
    assert body == "2026 ZOAC의 프리즈 시간이 21시 04분에서 21시 30분으로 변경되었습니다."
    assert time_update_notice_body("대회", [("start_at", at(), at().astimezone(timezone.utc))]) == ""


def test_dates_are_only_included_when_the_date_changes():
    assert time_update_notice_body("대회", [("start_at", at(), at(day=6))]) == "대회의 시작 시간이 09월 05일 21시 04분에서 09월 06일 21시 04분으로 변경되었습니다."
    assert time_update_notice_body("대회", [("end_at", at(), at(year=2027))]) == "대회의 종료 시간이 2026년 09월 05일 21시 04분에서 2027년 09월 05일 21시 04분으로 변경되었습니다."


def test_multiple_changes_use_short_separate_sentences():
    assert time_update_notice_body("대회", [
        ("start_at", at(hour=18), at(hour=19)),
        ("end_at", at(hour=22), at(hour=23)),
    ]) == "대회의 시작 시간이 18시 04분에서 19시 04분으로 변경되었습니다.\n종료 시간이 22시 04분에서 23시 04분으로 변경되었습니다."


LEGACY_TIME = "\n".join([
    "2026 ZOAC 운영시간이 변경되었습니다.", "",
    "- 오픈: 26년 09월 05일 18시 04분 KST -> 26년 09월 05일 18시 04분 KST",
    "- 프리즈: 26년 09월 05일 21시 04분 KST -> 26년 09월 05일 21시 30분 KST",
    "- 마감: 26년 09월 05일 22시 04분 KST -> 26년 09월 05일 22시 04분 KST",
    "", LEGACY_SCHEDULE_FOOTER,
])


def test_existing_time_notice_is_compact_and_keeps_manual_additions():
    expected = "2026 ZOAC의 프리즈 시간이 21시 04분에서 21시 30분으로 변경되었습니다."
    assert compact_automatic_notice(LEGACY_TIME) == expected
    assert compact_automatic_notice(LEGACY_TIME + "\n\n문의는 운영진에게 해 주세요.") == expected + "\n\n문의는 운영진에게 해 주세요."
    assert compact_automatic_notice(expected) == expected


@pytest.mark.parametrize("body", [None, "", "직접 작성한 긴급 안내입니다.\n내용을 유지하세요.", LEGACY_TIME.replace("26년 09월", "잘못된 날짜"), LEGACY_TIME.replace(LEGACY_SCHEDULE_FOOTER, "운영진이 직접 덧붙인 내용")])
def test_manual_or_unrecognized_notices_are_unchanged(body):
    assert compact_automatic_notice(body) == body


def legacy_event(target, target_at, remaining=None):
    time = target_at.astimezone(KST)
    formatted = f"{time.year}. {time.month}. {time.day}. {time:%H:%M} KST"
    label = "프리즈" if target == "freeze" else "종료"
    if remaining:
        first = f"{'스코어보드 프리즈' if target == 'freeze' else '대회 종료'}까지 {remaining} 남았습니다."
        last = "- 프리즈 이후 제출 결과는 대회 종료 전까지 스코어보드에 반영되지 않을 수 있습니다." if target == "freeze" else "- 종료 이후에는 제출이 제한될 수 있으니 남은 시간을 확인해 주세요."
    else:
        first = "스코어보드가 프리즈되었습니다." if target == "freeze" else "대회가 종료되었습니다."
        last = "- 프리즈 이후 제출 결과는 대회 종료 전까지 공개 스코어보드에 반영되지 않을 수 있습니다." if target == "freeze" else "- 종료 이후에는 제출이 제한됩니다."
    return f"{first}\n\n- {label} 시각: {formatted}\n{last}"


@pytest.mark.parametrize("target", ["freeze", "end"])
@pytest.mark.parametrize("remaining", ["30분", "10분", "5분", "1분", None])
def test_all_scheduled_templates_and_existing_notices_are_concise(target, remaining):
    title, body = scheduled_notice_copy(target, remaining)
    assert "\n" not in body and len(body) < 70
    assert "KST" not in body and "시각:" not in body
    legacy = legacy_event(target, at(), remaining)
    assert legacy_scheduled_notice(legacy) == (title, at(), body)
    assert compact_automatic_notice(legacy) == body


@pytest.mark.parametrize("access", ["전체 공개", "참가자 공개"])
def test_legacy_scoreboard_notice_does_not_claim_final_rankings_are_released(access):
    body = f"스코어보드가 공개되었습니다.\n\n- 공개 범위: {access}\n- 공개 시각: 2026. 9. 5. 22:04 KST"
    compact = compact_automatic_notice(body)
    assert "확인할 수 있습니다." in compact
    assert "공개되었습니다" not in compact


@pytest.fixture
def contest(monkeypatch):
    now = datetime(2035, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(import_module("app.services.store"), "now_utc", lambda: now)
    monkeypatch.setattr(settings, "feature_emergency_notice_auto", True)
    contest = store.create_contest(
        "자동 공지 테스트 " + uuid4().hex[:8], "테스트", "",
        start_at=now - timedelta(hours=1), freeze_at=now + timedelta(minutes=9),
        end_at=now + timedelta(hours=1), status=ContestStatus.RUNNING,
    )
    yield contest, now
    store.update_contest_settings(contest.contest_id, status=ContestStatus.ARCHIVED)


def notices(contest):
    return store.contest_notices_for_view(contest.contest_id, operator=True)


def test_worker_deduplicates_short_copy_but_allows_rescheduled_reminders(contest):
    contest, now = contest
    store.enqueue_due_contest_emergency_notices()
    original = notices(contest)
    assert len(original) == 1
    assert original[0].body == "스코어보드 프리즈까지 9분 남았습니다."
    assert original[0].body_template == countdown_template("freeze", contest.freeze_at)
    store.enqueue_due_contest_emergency_notices()
    assert len(notices(contest)) == 1
    store.update_contest_settings(contest.contest_id, freeze_at=now + timedelta(minutes=8))
    store.enqueue_due_contest_emergency_notices()
    assert len(notices(contest)) == 2
    assert {n.body for n in notices(contest)} == {"스코어보드 프리즈까지 9분 남았습니다.", "스코어보드 프리즈까지 8분 남았습니다."}


def test_legacy_reminder_is_not_reposted_after_deployment(contest):
    contest, _ = contest
    body = legacy_event("freeze", contest.freeze_at, "10분")
    notice = store.create_contest_notice(contest.contest_id, "스코어보드 프리즈 10분 전", body, emergency=True)
    store.enqueue_due_contest_emergency_notices()
    assert len(notices(contest)) == 1
    assert store.contests[contest.contest_id].emergency_notice == "스코어보드 프리즈까지 9분 남았습니다."
    with store._session() as db:
        assert db.get(ContestNoticeRow, notice.contest_notice_id).body == countdown_template("freeze", contest.freeze_at)
    assert notices(contest)[0].contest_notice_id == notice.contest_notice_id
    assert notices(contest)[0].published_at == notice.published_at
    store.enqueue_due_contest_emergency_notices()
    assert len(notices(contest)) == 1


def test_edited_automatic_notice_is_not_reposted(contest):
    contest, _ = contest
    store.enqueue_due_contest_emergency_notices()
    notice = notices(contest)[0]
    store.update_contest_notice(contest.contest_id, notice.contest_notice_id, body="운영진 수정 안내")
    store.enqueue_due_contest_emergency_notices()
    assert len(notices(contest)) == 1
    assert notices(contest)[0].body == "운영진 수정 안내"


def test_live_scoreboard_does_not_announce_a_freeze(contest):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, scoreboard_freeze_mode="live")
    store.enqueue_due_contest_emergency_notices()
    assert notices(contest) == []
    store.update_contest_settings(contest.contest_id, freeze_at=now - timedelta(minutes=1))
    store.enqueue_due_contest_emergency_notices()
    assert notices(contest) == []


@pytest.mark.parametrize("release_mode", ["manual", "manual_all", "immediate", "resolver"])
def test_end_notice_is_short_and_does_not_promise_unreleased_rankings(contest, release_mode):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, freeze_at=now - timedelta(hours=1), end_at=now - timedelta(minutes=1), scoreboard_access_after_end="public", scoreboard_release_mode=release_mode)
    store.enqueue_due_contest_emergency_notices()
    store.enqueue_due_contest_emergency_notices()
    assert [(notice.title, notice.body) for notice in notices(contest)] == [("대회 종료 안내", "대회가 종료되었습니다. 수고하셨습니다.")]
    assert store.contests[contest.contest_id].emergency_notice == "대회가 종료되었습니다. 수고하셨습니다."


def test_existing_schedule_notice_displays_compact_copy_without_changing_storage(contest):
    contest, _ = contest
    notice = store.create_contest_notice(contest.contest_id, "대회 운영 시간이 변경되었습니다", LEGACY_TIME, emergency=True)
    expected = "2026 ZOAC의 프리즈 시간이 21시 04분에서 21시 30분으로 변경되었습니다."
    assert notice.body == store.contests[contest.contest_id].emergency_notice == expected
    assert notices(contest)[0].title == "대회 일정 변경"
    with store._session() as db:
        assert db.get(ContestNoticeRow, notice.contest_notice_id).body == LEGACY_TIME


@pytest.mark.parametrize("minutes,title,body", [
    (31, None, None),
    (30, "대회 시작 안내", "대회 시작까지 30분 남았습니다."),
    (10, "대회 시작 안내", "대회 시작까지 10분 남았습니다."),
    (5, "대회 시작 안내", "대회 시작까지 5분 남았습니다."),
    (1, "대회 시작 안내", "대회 시작까지 1분 남았습니다."),
    (0, "대회 시작 안내", "대회가 시작되었습니다."),
    (-9, "대회 시작 안내", "대회가 시작되었습니다."),
    (-11, None, None),
])
def test_start_reminders_and_started_notice_are_published_once(contest, minutes, title, body):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, status=ContestStatus.OPEN,
        start_at=now + timedelta(minutes=minutes), freeze_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3))
    store.enqueue_due_contest_emergency_notices()
    store.enqueue_due_contest_emergency_notices()
    assert [(n.title, n.body) for n in notices(contest)] == ([(title, body)] if title else [])
    if body:
        assert store.contests[contest.contest_id].emergency_notice == body
        notice, = notices(contest)
        assert notice.emergency and notice.pinned
        assert notice.visibility == "participants"


@pytest.mark.parametrize("state", [ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD, ContestStatus.ENDED, ContestStatus.FINALIZED, ContestStatus.ARCHIVED])
def test_inactive_contests_do_not_announce_a_start(contest, state):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, status=state,
        start_at=now - timedelta(seconds=1), freeze_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3))
    store.enqueue_due_contest_emergency_notices()
    assert notices(contest) == []


def test_short_contest_start_is_visible_and_ended_contest_never_announces_its_recent_start(contest):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, status=ContestStatus.OPEN,
        start_at=now + timedelta(minutes=9), freeze_at=now + timedelta(minutes=10), end_at=now + timedelta(minutes=11))
    store.enqueue_due_contest_emergency_notices()
    assert [n.title for n in notices(contest)] == ["대회 시작 안내"]
    store.update_contest_settings(contest.contest_id, start_at=now, freeze_at=now + timedelta(minutes=1), end_at=now + timedelta(minutes=2))
    store.enqueue_due_contest_emergency_notices()
    assert store.contests[contest.contest_id].emergency_notice == "대회가 시작되었습니다."
    store.update_contest_settings(contest.contest_id, start_at=now - timedelta(minutes=2), freeze_at=now - timedelta(minutes=1), end_at=now)
    store.enqueue_due_contest_emergency_notices()
    assert store.contests[contest.contest_id].emergency_notice == "대회가 종료되었습니다. 수고하셨습니다."
    assert len([n for n in notices(contest) if n.title == "대회 시작 안내"]) == 2


def test_start_reminder_is_rescheduled_without_repeating_the_same_event(contest):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, status=ContestStatus.OPEN, start_at=now + timedelta(minutes=9))
    store.enqueue_due_contest_emergency_notices()
    store.update_contest_settings(contest.contest_id, start_at=now + timedelta(minutes=8))
    store.enqueue_due_contest_emergency_notices()
    store.enqueue_due_contest_emergency_notices()
    assert [n.title for n in notices(contest)] == ["대회 시작 안내", "대회 시작 안내"]


def test_auto_notice_switch_disables_start_notices_too(contest, monkeypatch):
    contest, now = contest
    store.update_contest_settings(contest.contest_id, status=ContestStatus.OPEN, start_at=now + timedelta(minutes=9))
    monkeypatch.setattr(settings, "feature_emergency_notice_auto", False)
    assert store.enqueue_due_contest_emergency_notices() == 0
    assert notices(contest) == []


def test_participant_banner_api_receives_each_start_freeze_and_end_event(contest, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    contest, now = contest
    start = now + timedelta(minutes=10)
    freeze, end = start + timedelta(hours=1), start + timedelta(hours=2)
    store.update_contest_settings(contest.contest_id, status=ContestStatus.OPEN,
        start_at=start, freeze_at=freeze, end_at=end, notice_access_after_end="public")
    client = TestClient(app)
    expected_events = [
        (now, "대회 시작까지 10분 남았습니다."),
        (start, "대회가 시작되었습니다."),
        (freeze - timedelta(minutes=10), "스코어보드 프리즈까지 10분 남았습니다."),
        (freeze, "스코어보드가 프리즈되었습니다. 이후 제출 결과는 스코어보드에서 잠시 숨겨집니다."),
        (end - timedelta(minutes=10), "대회 종료까지 10분 남았습니다."),
        (end, "대회가 종료되었습니다. 수고하셨습니다."),
    ]
    event_ids = []
    for clock, expected in expected_events:
        monkeypatch.setattr(import_module("app.services.store"), "now_utc", lambda: clock)
        store.enqueue_due_contest_emergency_notices()
        response = client.get(f"/api/public/contests/{contest.contest_id}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["contest"]["emergency_notice"] == expected
        event_ids.append({n.contest_notice_id for n in notices(contest)})
    assert len(notices(contest)) == 3
    assert all(event_ids[i] == event_ids[i + 1] for i in [0, 2, 4])


def test_countdown_keeps_one_notice_across_all_old_thresholds(contest, monkeypatch):
    contest, now = contest
    deadline = now + timedelta(minutes=30)
    store.update_contest_settings(contest.contest_id, freeze_at=deadline, end_at=now + timedelta(hours=2))
    first_id = None
    for elapsed in [0, 1, 60, 1200, 1500, 1740, 1800, 1801, 2400]:
        clock = now + timedelta(seconds=elapsed)
        monkeypatch.setattr(import_module("app.services.store"), "now_utc", lambda: clock)
        store.enqueue_due_contest_emergency_notices()
        current, = notices(contest)
        if first_id is None:
            first_id = current.contest_notice_id
        assert current.contest_notice_id == first_id
        assert current.body_template == countdown_template("freeze", deadline)
        if elapsed == 1:
            assert "29분 59초" in current.body
        if elapsed >= 1800:
            assert "프리즈되었습니다" in current.body


def test_migrating_old_notice_preserves_newer_manual_banner(contest):
    contest, _ = contest
    from app.services.automatic_notices import scheduled_notice_id
    title, body = scheduled_notice_copy("freeze", "10분")
    original = store.create_contest_notice(contest.contest_id, title, body, emergency=True,
        notice_id=scheduled_notice_id(contest.contest_id, title, contest.freeze_at))
    store.update_contest_settings(contest.contest_id, emergency_notice="운영진의 중요 안내")
    store.enqueue_due_contest_emergency_notices()
    current, = notices(contest)
    assert current.contest_notice_id == original.contest_notice_id
    assert current.body_template is not None
    assert store.contests[contest.contest_id].emergency_notice == "운영진의 중요 안내"
