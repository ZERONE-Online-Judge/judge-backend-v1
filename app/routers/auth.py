from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator
from starlette.concurrency import run_in_threadpool

from app.settings import settings
from app.services.access_logging import general_role, write_access_log
from app.services.presentation_access import is_presentation_email
from app.services.errors import AppError
from app.services.authz import bearer_token, require_staff
from app.services.responses import ok
from app.services.store import SessionConflictError, store

from app.routers.passkeys import router as passkey_router

router = APIRouter(tags=["auth"])
router.include_router(passkey_router)


@router.get("/notifications")
async def notifications(request: Request, contest_id: str | None = None, offset: int = 0, limit: int = 50):
    from app.services.notifications import list_notifications
    return ok(request, await run_in_threadpool(list_notifications, store, bearer_token(request), contest_id, offset, limit))


@router.post("/notifications/read-all")
async def read_all_notifications(request: Request, contest_id: str | None = None):
    from app.services.notifications import read_all
    return ok(request, await run_in_threadpool(read_all, store, bearer_token(request), contest_id))


@router.get("/notifications/rejudging")
async def rejudging_notifications(request: Request, contest_id: str | None = None, offset: int = 0, limit: int = 50):
    from app.services.rejudging_notifications import list_notifications
    return ok(request, await run_in_threadpool(list_notifications, store, bearer_token(request), contest_id, offset, limit))


@router.post("/notifications/rejudging/{notification_id}/read")
async def read_rejudging_notification(notification_id: str, request: Request, contest_id: str | None = None):
    from app.services.rejudging_notifications import update_notification
    return ok(request, await run_in_threadpool(update_notification, store, bearer_token(request), notification_id, "read", contest_id))


@router.delete("/notifications/rejudging/{notification_id}")
async def delete_rejudging_notification(notification_id: str, request: Request, contest_id: str | None = None):
    from app.services.rejudging_notifications import update_notification
    return ok(request, await run_in_threadpool(update_notification, store, bearer_token(request), notification_id, "delete", contest_id))


@router.post("/notifications/{notification_id}/{action}")
async def change_notification(notification_id: str, action: str, request: Request, contest_id: str | None = None):
    from app.services.notifications import update_notification
    if action not in {"read", "unread", "dismiss"}:
        raise AppError(404, "not_found", "Notification action not found.")
    return ok(request, await run_in_threadpool(update_notification, store, bearer_token(request), notification_id, action, contest_id))


@router.delete("/notifications/{notification_id}")
async def hide_notification(notification_id: str, request: Request, contest_id: str | None = None):
    from app.services.notifications import update_notification
    return ok(request, await run_in_threadpool(update_notification, store, bearer_token(request), notification_id, "delete", contest_id))


@router.get("/auth/session-events")
async def watch_session(request: Request):
    from app.services.session_events import resolve_session_watch, session_events

    watch = await run_in_threadpool(resolve_session_watch, bearer_token(request))
    if not watch:
        raise AppError(401, "authentication_required", "An active session is required.")
    return StreamingResponse(
        session_events(request, watch),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class StaffLoginRequest(BaseModel):
    email: EmailStr
    password: str


class StaffRefreshRequest(BaseModel):
    refresh_token: str


class StaffLogoutRequest(BaseModel):
    refresh_token: str | None = None


class StaffOtpRequest(BaseModel):
    email: EmailStr


class StaffOtpVerifyRequest(BaseModel):
    email: EmailStr
    otp_code: str


class GeneralOtpRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value):
        return value.lower()


class GeneralOtpVerifyRequest(GeneralOtpRequest):
    otp_code: str = Field(default="",max_length=6)
    force_new_session: bool = False


class GeneralLoginMethodRequest(BaseModel):
    email: EmailStr


class GeneralPasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    force_new_session: bool = False


class PasswordConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp_code: str = Field(pattern=r"^[0-9]{6}$")
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def valid_password(cls, value):
        from app.services.account_passwords import validate_password
        return validate_password(value)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class PasswordResetConfirm(PasswordConfirm):
    email: EmailStr


class GeneralPasswordOtpRequest(BaseModel):
    email: EmailStr
    password: str


class GeneralPasswordOtpVerifyRequest(BaseModel):
    email: EmailStr
    password: str
    otp_code: str = ""


class GeneralRefreshRequest(BaseModel):
    refresh_token: str


class GeneralLogoutRequest(BaseModel):
    refresh_token: str | None = None


@router.post("/auth/staff/login")
def staff_login(payload: StaffLoginRequest, request: Request):
    raise AppError(410, "staff_login_removed", "Staff login has been unified into general email OTP login.")


@router.post("/auth/staff/otp/request")
def staff_otp_request(payload: StaffOtpRequest, request: Request):
    raise AppError(410, "staff_login_removed", "Staff login has been unified into general email OTP login.")


@router.post("/auth/staff/otp/verify")
def staff_otp_verify(payload: StaffOtpVerifyRequest, request: Request):
    raise AppError(410, "staff_login_removed", "Staff login has been unified into general email OTP login.")


@router.get("/auth/staff/me")
def staff_me(request: Request):
    account = require_staff(request)
    return ok(request, account.model_dump(mode="json"))


@router.post("/auth/staff/logout")
def staff_logout(payload: StaffLogoutRequest, request: Request):
    revoked = store.revoke_staff_session(bearer_token(request), payload.refresh_token)
    return ok(request, {"revoked": revoked})


@router.api_route("/auth/staff/logout", methods=["GET", "DELETE"])
def staff_logout_compat(request: Request):
    revoked = store.revoke_staff_session(bearer_token(request), None)
    return ok(request, {"revoked": revoked})


@router.post("/auth/staff/refresh")
def staff_refresh(payload: StaffRefreshRequest, request: Request):
    refreshed = store.refresh_staff_session(payload.refresh_token)
    if not refreshed:
        raise AppError(401, "invalid_credentials", "Invalid refresh token.")
    return ok(request, refreshed)


@router.post("/auth/general/otp/request")
def general_otp_request(payload: GeneralOtpRequest, request: Request):
    from app.services.auth_rate_limit import consume
    if not settings.allow_empty_otp:
        consume('otp-mail-ip',request.client.host,1000,3600)
        consume('otp-mail-email',str(payload.email),10,3600)
    if is_presentation_email(str(payload.email)):
        raise AppError(401, "presentation_login_required", "프레젠테이션 전용 로그인으로 접속해 주세요.")
    retry_after = store.general_otp_retry_after_seconds(str(payload.email))
    if retry_after > 0:
        raise AppError(
            429,
            "otp_request_rate_limited",
            f"Please wait {retry_after} seconds before requesting another verification code.",
            {"retry_after_seconds": retry_after},
        )
    sent = store.create_general_otp(str(payload.email))
    if not sent:
        raise AppError(404, "not_found", "This email is not registered.")
    data = {"sent": True, "delivery": "email", "cooldown_seconds": settings.otp_request_cooldown_seconds}
    if settings.allow_empty_otp:
        data["demo_otp"] = sent
    return ok(request, data)


@router.post("/auth/general/otp/verify")
def general_otp_verify(payload: GeneralOtpVerifyRequest, request: Request, response: Response):
    from app.services.auth_rate_limit import check_otp
    if not (settings.allow_empty_otp and payload.otp_code==''):
        check_otp(str(payload.email),request.client.host)
    if is_presentation_email(str(payload.email)):
        raise AppError(401, "presentation_login_required", "프레젠테이션 전용 로그인으로 접속해 주세요.")
    try:
        session = store.verify_general_otp(
            str(payload.email),
            payload.otp_code,
            payload.force_new_session,
        )
    except SessionConflictError as exc:
        write_access_log(
            request,
            event_type="session_conflict",
            account_scope="general",
            email=str(payload.email),
            details=exc.details,
        )
        raise AppError(
            409,
            "session_conflict",
            "이미 다른 브라우저 또는 기기에서 로그인 중입니다.",
            exc.details,
        )
    if not session:
        write_access_log(
            request,
            event_type="login_failed",
            account_scope="general",
            email=str(payload.email),
        )
        raise AppError(401, "invalid_credentials", "Invalid email or verification code.")
    account = session.get("account", {})
    if not (settings.allow_empty_otp and payload.otp_code == ""):
        from app.services.login_security import remember
        remember(request, response, session)
    from app.services.shared_login import COOKIE, set_cookie
    set_cookie(response,session["refresh_token"],request.cookies.get(COOKIE))
    write_access_log(
        request,
        event_type="general_login",
        account_scope="general",
        email=account.get("email") or str(payload.email),
        display_name=account.get("display_name"),
        actor_role=general_role(session),
        details={"force_new_session": payload.force_new_session},
    )
    return ok(request, session)


@router.post("/auth/general/login-method")
def general_login_method(payload: GeneralLoginMethodRequest, request: Request):
    return ok(request, {"method": "otp"})


@router.post("/auth/general/password/login")
def general_password_login(payload: GeneralPasswordLoginRequest, request: Request, response: Response):
    from app.services import password_auth, login_security
    if is_presentation_email(str(payload.email)):
        raise AppError(401, "presentation_login_required", "프레젠테이션 전용 로그인으로 접속해 주세요.")
    try:
        session = password_auth.login(str(payload.email), payload.password, payload.force_new_session, request.client.host, request.cookies.get(login_security.cookie_name()))
    except SessionConflictError as exc:
        raise AppError(409,"session_conflict","이미 다른 브라우저 또는 기기에서 로그인 중입니다.",exc.details)
    except AppError as exc:
        if exc.code == "additional_verification_required":
            write_access_log(request, event_type="login_verification_required", account_scope="general", email=str(payload.email), details={"method": "password"})
        raise
    if not session:
        write_access_log(request,event_type="login_failed",account_scope="general",email=str(payload.email),details={"method":"password"})
        raise AppError(401,"invalid_credentials","이메일 또는 비밀번호가 올바르지 않습니다.")
    account = session.get("account",{})
    from app.services.shared_login import COOKIE, set_cookie
    set_cookie(response,session["refresh_token"],request.cookies.get(COOKIE))
    write_access_log(request,event_type="general_login",account_scope="general",email=account.get("email"),display_name=account.get("display_name"),actor_role=general_role(session),details={"method":"password","force_new_session":payload.force_new_session})
    return ok(request,session)


def password_email(request):
    token = bearer_token(request)
    session = store.get_general_by_access_token(token) if token else None
    if not session:
        raise AppError(401,"authentication_required","로그인이 필요합니다.")
    return session["account"]["email"].lower()


@router.get("/auth/general/password")
def password_status(request: Request):
    from app.services import password_auth
    email = password_email(request)
    return ok(request,{"has_password":password_auth.has_password(email)})


@router.post("/auth/general/password/request")
def password_request(request: Request):
    from app.services import password_auth
    email = password_email(request)
    return ok(request,password_auth.request_change(email,"set",request.client.host))


@router.post("/auth/general/password/confirm")
def password_confirm(payload: PasswordConfirm, request: Request, response: Response):
    from app.services import password_auth
    email = password_email(request)
    result = password_auth.confirm_change(email,"set",payload.otp_code,payload.new_password,request.client.host)
    from app.services.shared_login import clear_cookie
    clear_cookie(response)
    write_access_log(request,event_type="password_changed",account_scope="general",email=email)
    return ok(request,result)


@router.post("/auth/general/password/reset/request")
def password_reset_request(payload: PasswordResetRequest, request: Request):
    from app.services import password_auth
    return ok(request,password_auth.request_change(str(payload.email),"reset",request.client.host))


@router.post('/auth/general/login-options')
def general_login_options(payload: PasswordResetRequest, request: Request):
    from app.services.password_auth import login_options
    return ok(request, login_options(str(payload.email),request.client.host))


@router.post("/auth/general/password/reset/confirm")
def password_reset_confirm(payload: PasswordResetConfirm, request: Request, response: Response):
    from app.services import password_auth
    result = password_auth.confirm_change(str(payload.email),"reset",payload.otp_code,payload.new_password,request.client.host)
    from app.services.shared_login import clear_cookie
    clear_cookie(response)
    write_access_log(request,event_type="password_reset",account_scope="general",email=str(payload.email))
    return ok(request,result)


@router.post("/auth/general/password/otp/request")
def general_password_otp_request(payload: GeneralPasswordOtpRequest, request: Request):
    raise AppError(410, "password_login_removed", "Password login has been removed. Use email OTP login.")


@router.post("/auth/general/password/otp/verify")
def general_password_otp_verify(payload: GeneralPasswordOtpVerifyRequest, request: Request):
    raise AppError(410, "password_login_removed", "Password login has been removed. Use email OTP login.")


@router.get("/auth/general/me")
def general_me(request: Request):
    token = bearer_token(request)
    session = store.get_general_by_access_token(token) if token else None
    if not session:
        raise AppError(401, "authentication_required", "General access token is required.")
    return ok(request, session)


@router.post("/auth/general/refresh")
def general_refresh(request: Request,response: Response,payload: GeneralRefreshRequest | None = None):
    from app.services.shared_login import COOKIE,set_cookie
    token=request.cookies.get(COOKIE) or (payload.refresh_token if payload else None)
    refreshed = store.refresh_general_session(token) if token else None
    if not refreshed:
        raise AppError(401, "invalid_credentials", "Invalid refresh token.")
    account = refreshed.get("account", {})
    if not request.cookies.get(COOKIE):
        set_cookie(response,refreshed['refresh_token'])
    write_access_log(
        request,
        event_type="general_refresh",
        account_scope="general",
        email=account.get("email"),
        display_name=account.get("display_name"),
        actor_role=general_role(refreshed),
    )
    return ok(request, refreshed)


@router.post("/auth/general/logout")
def general_logout(payload: GeneralLogoutRequest, request: Request,response:Response):
    token = bearer_token(request)
    session = store.get_general_by_access_token(token) if token else None
    from app.services.shared_login import COOKIE,clear_cookie
    common=request.cookies.get(COOKIE)
    revoked = store.revoke_general_session(None if common else token, common or payload.refresh_token)
    clear_cookie(response)
    if revoked:
        account = session.get("account", {}) if session else {}
        write_access_log(
            request,
            event_type="logout",
            account_scope="general",
            email=account.get("email"),
            display_name=account.get("display_name"),
            actor_role=general_role(session),
        )
    return ok(request, {"revoked": revoked})


@router.post('/auth/general/sso')
def restore_shared_login(request:Request,response:Response,payload:GeneralLogoutRequest | None = None):
    from app.services.shared_login import COOKIE,set_cookie
    origin=request.headers.get('origin')
    if settings.app_env=='production' and origin and origin.rstrip('/')!=settings.public_base_url.rstrip('/'):
        raise AppError(403,'origin_denied','허용되지 않은 로그인 요청입니다.')
    token=request.cookies.get(COOKIE) or (payload.refresh_token if payload else None)
    result=store.refresh_general_session(token) if token else None
    if not result:
        raise AppError(401,'authentication_required','ZOJ 로그인 세션이 없습니다.')
    if not request.cookies.get(COOKIE):
        set_cookie(response,result['refresh_token'])
    return ok(request,result)


@router.api_route("/auth/general/logout", methods=["GET", "DELETE"])
@router.api_route("/auth/logout", methods=["GET", "POST", "DELETE"])
def general_logout_compat(request: Request, response: Response):
    token = bearer_token(request)
    session = store.get_general_by_access_token(token) if token else None
    from app.services.shared_login import COOKIE, clear_cookie
    common = request.cookies.get(COOKIE)
    revoked = store.revoke_general_session(None if common else token, common)
    clear_cookie(response)
    if revoked:
        account = session.get("account", {}) if session else {}
        write_access_log(
            request,
            event_type="logout",
            account_scope="general",
            email=account.get("email"),
            display_name=account.get("display_name"),
            actor_role=general_role(session),
        )
    return ok(request, {"revoked": revoked})


@router.post("/auth/general/contests/{contest_id}/participant-session")
def general_participant_session(contest_id: str, request: Request):
    token = bearer_token(request)
    session = store.get_general_by_access_token(token) if token else None
    if not session:
        raise AppError(401, "authentication_required", "General access token is required.")
    verified = store.issue_participant_session_for_general(
        session["account"]["email"],
        contest_id,
        token,
    )
    if not verified:
        raise AppError(403, "scope_denied", "This account is not registered as a participant for the contest.")
    team, member, division, access_token = verified
    write_access_log(
        request,
        event_type="participant_session_issued",
        account_scope="participant",
        email=session["account"]["email"],
        display_name=session["account"].get("display_name"),
        contest_id=contest_id,
        participant_team_id=team.participant_team_id,
        team_name=team.team_name,
        team_member_id=member.team_member_id,
        member_name=member.name,
        actor_role="participant",
    )
    return ok(
        request,
        {
            "access_token": access_token,
            "team": team.model_dump(mode="json"),
            "member": member.model_dump(mode="json"),
            "division": division.model_dump(mode="json"),
        },
    )


class ParticipantPreviewSessionRequest(BaseModel):
    division_id: str


@router.get("/auth/general/contests/{contest_id}/participant-preview")
def participant_preview_options(contest_id: str, request: Request):
    from app.services.participant_preview import get_preview_participant, preview_account
    require_staff(request)
    token = bearer_token(request)
    if not token or not preview_account(contest_id, token):
        raise AppError(403, "scope_denied", "참가자 미리보기 권한이 필요합니다.")
    contest = store.contests.get(contest_id)
    if not contest:
        raise AppError(404, "not_found", "Contest not found.")
    participant = get_preview_participant(contest_id, token)
    return ok(request, {"contest": contest.model_dump(mode="json"), "divisions": [division.model_dump(mode="json") for division in store.contest_divisions(contest_id)], "is_preview": True, "selected_division_id": participant["division"].division_id if participant else None})


@router.post("/auth/general/contests/{contest_id}/participant-preview-session")
def participant_preview_session(contest_id: str, payload: ParticipantPreviewSessionRequest, request: Request):
    from app.services.participant_preview import select_preview_division
    require_staff(request)
    token = bearer_token(request)
    try:
        participant = select_preview_division(contest_id, payload.division_id, token) if token else None
    except ValueError:
        raise AppError(404, "not_found", "Contest division not found.")
    if not participant:
        raise AppError(403, "scope_denied", "참가자 미리보기 권한이 필요합니다.")
    return ok(request, {
        "access_token": token, "team": participant["team"].model_dump(mode="json"),
        "member": participant["member"].model_dump(mode="json"), "division": participant["division"].model_dump(mode="json"),
        "is_preview": True, "workspace_path": f"/contests/{contest_id}/divisions/{payload.division_id}/workspace",
    })
