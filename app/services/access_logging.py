from typing import Any
from collections import OrderedDict
from hashlib import sha256
from threading import Lock
from time import monotonic

from fastapi import Request

from app.services.store import store
from app.settings import settings

_operator_visits: OrderedDict[str, float] = OrderedDict()
_operator_visit_lock = Lock()
OPERATOR_ACCESS_WINDOW_SECONDS = 30 * 60


def record_operator_access(request: Request, contest_id: str, account, token: str) -> None:
    """One successful contest access per session/IP every 30 minutes, not per poll."""
    digest = sha256(f"{contest_id}:{token}:{client_ip(request)}".encode()).hexdigest()
    now = monotonic()
    with _operator_visit_lock:
        if _operator_visits.get(digest, 0) > now:
            return
        _operator_visits[digest] = now + OPERATOR_ACCESS_WINDOW_SECONDS
        _operator_visits.move_to_end(digest)
        while len(_operator_visits) > 4096:
            _operator_visits.popitem(last=False)
    if settings.redis_url:
        try:
            from app.services.result_cache import _client
            if not _client(settings.redis_url).set(f"zoj:operator-access:v1:{digest}", "1", nx=True, ex=OPERATOR_ACCESS_WINDOW_SECONDS):
                return
        except Exception:
            # Bounded local deduplication remains available when Redis is down.
            pass
    write_access_log(request, event_type="operator_access", account_scope="staff",
                     email=str(account.email), display_name=account.display_name,
                     contest_id=contest_id,
                     actor_role="service_master" if account.is_service_master else "operator",
                     details={"path": request.url.path, "deduplication_minutes": 30})


def client_ip(request: Request) -> str | None:
    # Uvicorn applies the configured trusted-proxy boundary before ASGI.
    # Reading the leftmost forwarding header here would allow forged audit IPs.
    return request.client.host if request.client else None


def general_role(session: dict | None) -> str | None:
    if not session:
        return None
    operator_session = session.get("operator_session")
    if isinstance(operator_session, dict):
        staff = operator_session.get("staff")
        if isinstance(staff, dict):
            return "service_master" if staff.get("is_service_master") else "operator"
    if session.get("participant_contests"):
        return "participant"
    return "general"


def write_access_log(
    request: Request,
    *,
    event_type: str,
    account_scope: str,
    email: str | None = None,
    display_name: str | None = None,
    contest_id: str | None = None,
    participant_team_id: str | None = None,
    team_name: str | None = None,
    team_member_id: str | None = None,
    member_name: str | None = None,
    actor_role: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        contest = store.contests.get(contest_id) if contest_id else None
        store.append_access_log(
            event_type=event_type,
            account_scope=account_scope,
            email=email,
            display_name=display_name,
            contest_id=contest_id,
            contest_title=contest.title if contest else None,
            participant_team_id=participant_team_id,
            team_name=team_name,
            team_member_id=team_member_id,
            member_name=member_name,
            actor_role=actor_role,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
            details=details,
        )
    except Exception:
        return
