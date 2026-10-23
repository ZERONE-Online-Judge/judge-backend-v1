"""Passkey management uses bearer authentication, never an ambient SSO cookie."""

import json
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from app.services import passkeys, login_security
from app.services.access_logging import write_access_log, general_role
from app.services.authz import bearer_token
from app.services.errors import AppError
from app.services.responses import ok
from app.services.store import SessionConflictError

router = APIRouter(prefix="/auth/general", tags=["passkeys"])


class Ceremony(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=32, max_length=128)
    credential: dict

    @field_validator("credential")
    @classmethod
    def bounded_credential(cls, value):
        if (
            value.get("type") != "public-key"
            or not isinstance(value.get("id"), str)
            or not 1 <= len(value["id"]) <= 1024
            or not isinstance(value.get("response"), dict)
        ):
            raise ValueError("Invalid credential shape")
        response = value["response"]
        if not isinstance(response.get("clientDataJSON"), str):
            raise ValueError("Invalid client data")
        if response.get("userHandle") is not None and not isinstance(
            response["userHandle"], str
        ):
            raise ValueError("Invalid user handle")
        if len(json.dumps(value)) > 32768:
            raise ValueError("Credential is too large")
        return value


class Registration(Ceremony):
    name: str = Field(default="내 패스키", max_length=80)


class Authentication(Ceremony):
    force_new_session: bool = False


class LoginOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class Removal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_id: str = Field(min_length=1, max_length=1024)


class EmailVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=32, max_length=128)
    code: str = Field(pattern=r"^[0-9]{6}$")


def log_management(request, event):
    from app.database import SessionLocal

    with SessionLocal() as db:
        session, _ = login_security.session_context(db, bearer_token(request))
        email = session.email
    write_access_log(request, event_type=event, account_scope="general", email=email)


@router.get("/passkeys")
def list_passkeys(request: Request):
    return ok(request, passkeys.list_passkeys(bearer_token(request)))


@router.post("/passkeys/register/options")
def register_options(request: Request):
    return ok(
        request,
        passkeys.registration_options(bearer_token(request), request.client.host),
    )


@router.post("/passkeys/register/verify")
def register(payload: Registration, request: Request):
    result = passkeys.register(
        bearer_token(request), payload.challenge_id, payload.credential, payload.name
    )
    log_management(request, "passkey_registered")
    return ok(request, result)


@router.post("/passkeys/remove")
def remove(payload: Removal, request: Request):
    result = passkeys.remove(bearer_token(request), payload.credential_id)
    log_management(request, "passkey_removed")
    return ok(request, result)


@router.post("/passkeys/login/options")
def login_options(request: Request, payload: LoginOptions | None = None):
    # Older clients can finish signing in during the rolling frontend upgrade.
    email = str(payload.email).lower() if payload else None
    return ok(request, passkeys.authentication_options(request.client.host, email))


@router.post("/passkeys/login/verify")
def login(payload: Authentication, request: Request, response: Response):
    try:
        session = passkeys.authenticate(
            payload.challenge_id,
            payload.credential,
            payload.force_new_session,
            request.client.host,
        )
    except SessionConflictError as exc:
        raise AppError(
            409,
            "session_conflict",
            "이미 다른 브라우저 또는 기기에서 로그인 중입니다.",
            exc.details,
        )
    except AppError as exc:
        write_access_log(
            request,
            event_type="login_failed",
            account_scope="general",
            details={"method": "passkey"},
        )
        if exc.code == "invalid_passkey":
            raise AppError(401, exc.code, exc.message)
        raise
    from app.services.shared_login import COOKIE, set_cookie

    set_cookie(response, session["refresh_token"], request.cookies.get(COOKIE))
    login_security.remember(request, response, session)
    write_access_log(
        request,
        event_type="general_login",
        account_scope="general",
        email=session["account"]["email"],
        actor_role=general_role(session),
        details={"method": "passkey", "force_new_session": payload.force_new_session},
    )
    return ok(request, session)


@router.post("/security/otp/request")
def request_email(request: Request):
    return ok(
        request,
        passkeys.request_email_verification(bearer_token(request), request.client.host),
    )


@router.post("/security/otp/verify")
def verify_email(payload: EmailVerification, request: Request):
    return ok(
        request,
        passkeys.verify_email(
            bearer_token(request),
            payload.challenge_id,
            payload.code,
            request.client.host,
        ),
    )
