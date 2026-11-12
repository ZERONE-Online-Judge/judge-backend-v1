"""Contest-only restrictions; account sessions and stored results remain intact."""
from datetime import timezone

from app.services.errors import AppError


def require_participant_access(team, member) -> None:
    for scope, target in (("team", team), ("member", member)):
        if target.access_blocked:
            blocked_at = target.access_blocked_at
            if blocked_at is not None and blocked_at.tzinfo is None:
                blocked_at = blocked_at.replace(tzinfo=timezone.utc)
            raise AppError(403, "participant_access_blocked",
                "운영진이 이 대회의 접근을 차단했습니다. 운영진에게 문의해 주세요.",
                {"scope": scope, "reason": target.access_block_reason,
                 "blocked_at": blocked_at.isoformat() if blocked_at else None})
