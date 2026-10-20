from fastapi import Request

from app.models import ContestStatus, StaffAccount
from app.services.errors import authentication_required, not_found, permission_denied, scope_denied
from app.services.store import store


def bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_staff(request: Request) -> StaffAccount:
    token = bearer_token(request)
    account = store.get_staff_by_access_token(token) if token else None
    if not account and token:
        account = store.get_staff_by_general_access_token(token)
    if not account:
        raise authentication_required("Staff or general operator access token is required.")
    return account


def require_service_master(request: Request) -> StaffAccount:
    account = require_staff(request)
    if not account.is_service_master:
        raise permission_denied("Service master permission is required.")
    return account


def has_contest_permission(account: StaffAccount, contest_id: str, permission: str) -> bool:
    scopes = account.contest_scopes.get(contest_id, [])
    if (
        account.is_service_master or "contest.*" in scopes or permission in scopes
        or (permission == "contest.problem.view" and "contest.problem.review" in scopes)
    ):
        return True
    if not scopes:
        return False
    if permission in {"contest.notice.view", "contest.board.question.view"}:
        return True
    if permission == "contest.problem.view":
        contest = store.get_contest(contest_id)
        return bool(contest and contest.status == ContestStatus.RUNNING)
    return False


def is_contest_master(account: StaffAccount, contest_id: str) -> bool:
    return account.is_service_master or "contest.*" in account.contest_scopes.get(contest_id, [])


def require_contest_staff(request: Request, contest_id: str, *permissions: str) -> StaffAccount:
    account = require_staff(request)
    if not is_contest_master(account, contest_id) and not any(
        has_contest_permission(account, contest_id, permission) for permission in (permissions or ("contest.view",))
    ):
        raise scope_denied()
    if store.is_contest_deleted(contest_id):
        raise not_found("대회를 찾을 수 없습니다.")
    request.state.operator_access = (contest_id, account)
    return account


def require_participant(request: Request, contest_id: str) -> dict:
    token = bearer_token(request)
    session = store.get_participant_by_access_token(contest_id, token) if token else None
    if not session:
        raise authentication_required("Participant access token is required.")
    return session
