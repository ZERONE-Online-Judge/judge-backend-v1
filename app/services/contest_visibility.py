from datetime import datetime, timezone

from app.models import ContestResourceAccess, ContestStatus, ContestVisibility, now_utc
from app.services.errors import AppError


RESOURCE_ACCESS_FIELDS = tuple(f"{resource}_access_after_end" for resource in (
    "problem", "scoreboard", "submission", "board", "notice", "editorial",
))


def contest_hidden_by_status(contest) -> bool:
    return contest.status in {ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD, ContestStatus.SCHEDULED}


def contest_is_public(contest) -> bool:
    visibility = contest.visibility_after_end if contest_has_ended(contest) else contest.visibility
    return visibility == ContestVisibility.PUBLIC


def normalize_private_resources(contest) -> None:
    if contest.visibility_after_end == ContestVisibility.PRIVATE:
        for field in RESOURCE_ACCESS_FIELDS:
            if getattr(contest, field) == ContestResourceAccess.PUBLIC:
                setattr(contest, field, ContestResourceAccess.PARTICIPANTS.value)


def visible_private_contest_ids(token: str | None) -> set[str]:
    """Resolve current server-side memberships, never trust client contest lists."""
    if not token:
        return set()
    from app.services.store import store
    from app.services.security import decode_session_token

    # Resolve membership after scheduled status transitions, so the first request
    # at the start of a private contest does not use a hidden pre-start profile.
    store.refresh_contest_statuses()
    profile = store.get_general_by_access_token(token)
    if profile:
        return {
            item["contest"]["contest_id"]
            for key in ("participant_contests", "operator_contests")
            for item in profile[key]
        }
    staff = store.get_staff_by_access_token(token)
    if staff:
        if staff.is_service_master:
            return set(store.contests)
        return {cid for cid, scopes in staff.contest_scopes.items() if scopes}
    claims = decode_session_token(token, "participant_access")
    cid = claims.get("contest_id") if claims else None
    if cid and store.get_participant_by_access_token(cid, token, enforce_access=False):
        return {cid}
    return set()


def contest_has_ended(contest) -> bool:
    if contest.status in {ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD}:
        return False
    return contest.status in {ContestStatus.ENDED, ContestStatus.FINALIZED, ContestStatus.ARCHIVED} or now_utc() >= _schedule_instant(contest.end_at)


def _schedule_instant(value):
    # SQLite returns naive UTC datetimes, while API models include timezones.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def require_unchanged_ended_schedule(contest, updates: dict) -> None:
    """Check the saved contest before applying a requested status or schedule."""
    if not contest_has_ended(contest):
        return
    changed = [field for field in ("start_at", "freeze_at", "end_at")
        if field in updates and _schedule_instant(updates[field]) != _schedule_instant(getattr(contest, field))]
    if changed:
        raise AppError(409, "contest_schedule_locked",
            "종료된 대회의 시작·프리즈·종료 시각은 변경할 수 없습니다. 다른 설정은 변경할 수 있습니다.",
            {"fields": changed})


def contest_payload_for_view(contest, participant: dict | None = None) -> dict:
    payload = contest.model_dump(mode="json")
    # The overview and public problem workspace must not bypass notice access.
    if (
        contest_has_ended(contest)
        and contest.notice_access_after_end != ContestResourceAccess.PUBLIC
        and not participant
    ):
        payload["emergency_notice"] = None
        payload["emergency_notice_template"] = None
    return payload
