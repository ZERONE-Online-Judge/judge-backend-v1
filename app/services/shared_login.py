"""One persisted ZOJ session, shared by the trusted ZOJ and Editor domains."""

import time
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import GeneralSessionRow, IdentityAccountRow, StaffAccountRow
from app.services.errors import AppError
from app.services.security import decode_session_token, token_hash
from app.services.store import _aware, store
from app.settings import settings

COOKIE = "zoj_sso"


def domain():
    return settings.sso_cookie_domain or (
        ".zoj.kr" if settings.app_env == "production" else None
    )


def set_cookie(response, token, previous=None):
    if previous and previous != token:
        store.revoke_general_session(None, previous)
    claims = decode_session_token(token, "general_refresh")
    if not claims:
        raise AppError(401, "authentication_required", "로그인이 필요합니다.")
    response.set_cookie(
        COOKIE,
        token,
        max_age=max(0, int(claims["exp"]) - int(time.time())),
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        domain=domain(),
        path="/",
    )


def clear_cookie(response):
    response.delete_cookie(
        COOKIE,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        domain=domain(),
        path="/",
    )


def resolve(token):
    if not token or not decode_session_token(token, "general_refresh"):
        raise AppError(401, "authentication_required", "ZOJ 로그인 세션이 필요합니다.")
    with SessionLocal() as db:
        session = db.scalar(
            select(GeneralSessionRow).where(
                GeneralSessionRow.refresh_token_hash == token_hash(token)
            )
        )
        if (
            not session
            or session.revoked_at
            or _aware(session.refresh_expires_at) <= now_utc()
        ):
            raise AppError(
                401,
                "authentication_required",
                "로그인 연결이 해제되었습니다. 다시 로그인하세요.",
            )
        account = db.scalar(
            select(IdentityAccountRow).where(
                IdentityAccountRow.email == session.email.lower()
            )
        )
        if not account:
            profile = store._general_profile(db, session.email)
            if not profile:
                raise AppError(
                    401, "authentication_required", "계정을 확인할 수 없습니다."
                )
            account = IdentityAccountRow(
                email=session.email.lower(),
                display_name=profile["account"]["display_name"],
            )
            db.add(account)
            try:
                db.commit()
                db.refresh(account)
            except IntegrityError:
                db.rollback()
                account = db.scalar(
                    select(IdentityAccountRow).where(
                        IdentityAccountRow.email == session.email.lower()
                    )
                )
                if not account:
                    raise AppError(
                        401, "authentication_required", "계정을 확인할 수 없습니다."
                    )
        # Resolve the central role on every request so grants and revocations
        # apply to existing Editor sessions without trusting client claims.
        is_service_master = db.scalar(
            select(StaffAccountRow.is_service_master).where(
                func.lower(StaffAccountRow.email) == account.email.lower()
            )
        )
        return {
            "account_id": account.account_id,
            "email": account.email,
            "display_name": account.display_name,
            "is_service_master": bool(is_service_master),
            "session_id": session.general_session_id,
        }


def editor_result(session, previous=None):
    token = session["refresh_token"]
    if previous and previous != token:
        store.revoke_general_session(None, previous)
    user = resolve(token)
    user.pop("session_id")
    claims = decode_session_token(token, "general_refresh")
    return {
        "user": user,
        "session_token": token,
        "expires_in": max(0, int(claims["exp"]) - int(time.time())),
    }
