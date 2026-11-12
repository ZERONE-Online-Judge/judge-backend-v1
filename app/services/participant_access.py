"""Contest-only restrictions; account sessions and stored results remain intact."""
from datetime import timezone

from app.services.errors import AppError


def require_participant_access(team, member) -> None:
    if getattr(team, "withdrawn", False):
        withdrawn_at = team.withdrawn_at
        if withdrawn_at is not None and withdrawn_at.tzinfo is None:
            withdrawn_at = withdrawn_at.replace(tzinfo=timezone.utc)
        raise AppError(403, "participant_access_blocked",
            "기권 처리된 팀은 이 대회에 접근할 수 없습니다. 운영진에게 문의해 주세요.",
            {"scope": "team", "withdrawn": True, "reason": team.withdrawal_reason,
             "blocked_at": withdrawn_at.isoformat() if withdrawn_at else None})
    for scope, target in (("team", team), ("member", member)):
        if target.access_blocked:
            blocked_at = target.access_blocked_at
            if blocked_at is not None and blocked_at.tzinfo is None:
                blocked_at = blocked_at.replace(tzinfo=timezone.utc)
            raise AppError(403, "participant_access_blocked",
                "운영진이 이 대회의 접근을 차단했습니다. 운영진에게 문의해 주세요.",
                {"scope": scope, "reason": target.access_block_reason,
                 "blocked_at": blocked_at.isoformat() if blocked_at else None})
