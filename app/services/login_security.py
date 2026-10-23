"""Strong authentication freshness and bounded, browser-and-IP login trust."""

import hashlib
import hmac
import ipaddress
import secrets
import time

from sqlalchemy import delete, select
from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import (
    GeneralSessionRow,
    IdentityAccountRow,
    SessionVerificationRow,
    TrustedEnvironmentRow,
)
from app.services.errors import AppError
from app.services.security import token_hash
from app.settings import settings

TRUST_SECONDS = 30 * 24 * 3600
FRESH_SECONDS = 300


def cookie_name():
    return "__Host-zoj_device" if settings.app_env == "production" else "zoj_device"


def ip_key(value):
    # Uvicorn resolves request.client only from configured trusted proxy peers.
    # Never read X-Forwarded-For directly in authentication decisions.
    try:
        normalized = str(ipaddress.ip_address(value))
    except ValueError:
        return None
    return hmac.new(
        settings.auth_token_secret.encode(),
        ("login-ip:" + normalized).encode(),
        hashlib.sha256,
    ).hexdigest()


def is_trusted(db, email, device_token, client_ip):
    key = ip_key(client_ip)
    if not device_token or not key or len(device_token) > 128:
        return False
    row = db.get(TrustedEnvironmentRow, (email.lower(), token_hash(device_token), key))
    return bool(row and row.expires_at > int(time.time()))


def remember(request, response, login_session):
    email = login_session["account"]["email"]
    key = ip_key(request.client.host)
    if not key:
        return
    token = request.cookies.get(cookie_name())
    if not token or len(token) > 128:
        token = secrets.token_urlsafe(32)
    at = int(time.time())
    with SessionLocal() as db:
        session_context(db, login_session["access_token"], fresh=True)
        db.execute(
            delete(TrustedEnvironmentRow).where(TrustedEnvironmentRow.expires_at <= at)
        )
        row = db.get(TrustedEnvironmentRow, (email.lower(), token_hash(token), key))
        if row:
            row.expires_at = at + TRUST_SECONDS
        else:
            db.add(
                TrustedEnvironmentRow(
                    email=email.lower(),
                    device_hash=token_hash(token),
                    ip_hash=key,
                    expires_at=at + TRUST_SECONDS,
                )
            )
        db.commit()
    response.set_cookie(
        cookie_name(),
        token,
        max_age=TRUST_SECONDS,
        secure=settings.app_env == "production",
        httponly=True,
        samesite="lax",
        path="/",
    )


def session_context(db, token, *, fresh=False):
    from app.services.security import decode_session_token
    from app.services.store import _aware

    if not token or not decode_session_token(token, "general_access"):
        raise AppError(401, "authentication_required", "로그인이 필요합니다.")
    session = db.scalar(
        select(GeneralSessionRow).where(
            GeneralSessionRow.access_token_hash == token_hash(token),
            GeneralSessionRow.revoked_at.is_(None),
        )
    )
    if not session or _aware(session.access_expires_at) <= now_utc():
        raise AppError(401, "authentication_required", "다시 로그인해 주세요.")
    if fresh:
        # Account -> session -> credential is the common management lock order.
        db.scalar(
            select(IdentityAccountRow)
            .where(IdentityAccountRow.email == session.email.lower())
            .with_for_update()
        )
        session = db.scalar(
            select(GeneralSessionRow)
            .where(GeneralSessionRow.general_session_id == session.general_session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if session.revoked_at or _aware(session.access_expires_at) <= now_utc():
            raise AppError(401, "authentication_required", "다시 로그인해 주세요.")
    proof = db.get(SessionVerificationRow, session.general_session_id)
    recent = bool(proof and proof.verified_at > int(time.time()) - FRESH_SECONDS)
    if fresh and not recent:
        raise AppError(
            403,
            "recent_verification_required",
            "패스키를 변경하려면 이메일로 본인 인증을 해주세요.",
        )
    return session, recent


def mark_verified(db, session_id):
    row = db.get(SessionVerificationRow, session_id)
    if row:
        row.verified_at = int(time.time())
    else:
        db.add(
            SessionVerificationRow(session_id=session_id, verified_at=int(time.time()))
        )
