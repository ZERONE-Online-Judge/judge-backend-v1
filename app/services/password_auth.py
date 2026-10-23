import hashlib
import hmac
import secrets
import time
from sqlalchemy import delete, select, update
from app.database import SessionLocal
from app.orm_models import (
    AccountPasswordRow,
    PasswordChallengeRow,
    IdentityAccountRow,
    PasskeyRow,
)
from app.services.account_passwords import hash_password, verify_password
from app.services.auth_rate_limit import consume
from app.services.errors import AppError
from app.services.mail_templates import render_branded_email
from app.services.store import SessionConflictError, store
from app.settings import settings


def proof(email, purpose, code):
    return hmac.new(
        settings.auth_token_secret.encode(),
        ("password:" + purpose + ":" + email + ":" + code).encode(),
        hashlib.sha256,
    ).hexdigest()


def has_password(email):
    with SessionLocal() as db:
        return db.get(AccountPasswordRow, email.lower()) is not None


def login_options(email, client_ip):
    # Shared campus IPs must accommodate the common first login step.
    # Return available methods only, never account roles or profile.
    email = email.strip().lower()
    consume("login-options-ip", client_ip, 1200, 300)
    consume("login-options-email", email, 10, 60)
    with SessionLocal() as db:
        passkey_available = (
            db.scalar(
                select(PasskeyRow.credential_id)
                .join(
                    IdentityAccountRow,
                    PasskeyRow.account_id == IdentityAccountRow.account_id,
                )
                .where(IdentityAccountRow.email == email)
                .limit(1)
            )
            is not None
        )
        return {
            "password_available": db.get(AccountPasswordRow, email) is not None,
            "passkey_available": passkey_available,
        }


def request_change(email, purpose, client_ip):
    email = email.lower()
    consume("pwd-mail-ip", client_ip, 1000, 3600)
    consume("pwd-mail-email", email, 10, 3600)
    consume("pwd-mail-cooldown", email, 1, 30)
    with store._session() as db:
        credential = db.get(AccountPasswordRow, email)
        if not store._has_general_login_identity(db, email) or (
            purpose == "reset" and not credential
        ):
            return {"requested": True, "cooldown_seconds": 30, "valid_seconds": 300}
        code = f"{secrets.randbelow(1_000_000):06d}"
        row = db.get(PasswordChallengeRow, (email, purpose))
        if not row:
            row = PasswordChallengeRow(email=email, purpose=purpose)
            db.add(row)
        at = int(time.time())
        row.code_digest = proof(email, purpose, code)
        row.expected_password_hash = credential.password_hash if credential else ""
        row.requested_at, row.expires_at, row.attempts = at, at + 300, 0
        db.commit()
    action = "비밀번호 재설정" if purpose == "reset" else "비밀번호 등록·변경"
    subject = "[ZOJ] " + action + " 인증번호"
    html = render_branded_email(
        title=action + " 인증번호",
        preheader=subject,
        body=[
            "아래 인증번호를 ZOJ 화면에 입력하세요.",
            "요청하지 않았다면 이 메일을 무시하세요.",
        ],
        verification_code=code,
        verification_hint="5분 동안 한 번 사용할 수 있습니다.",
    )
    store.enqueue_mail(
        "account_password_otp",
        email,
        subject,
        f"{action} 인증번호: {code}\n5분 동안 한 번 사용할 수 있습니다.",
        html,
    )
    return {"requested": True, "cooldown_seconds": 30, "valid_seconds": 300}


def confirm_change(email, purpose, code, password, client_ip):
    email = email.lower()
    consume("pwd-confirm-ip", client_ip, 200, 3600)
    encoded = hash_password(password)
    at = int(time.time())
    with store._session() as db:
        row = db.scalar(
            select(PasswordChallengeRow)
            .where(
                PasswordChallengeRow.email == email,
                PasswordChallengeRow.purpose == purpose,
            )
            .with_for_update()
        )
        credential = db.scalar(
            select(AccountPasswordRow)
            .where(AccountPasswordRow.email == email)
            .with_for_update()
        )
        if (
            not row
            or row.expires_at <= at
            or row.attempts >= 5
            or not store._has_general_login_identity(db, email)
        ):
            raise AppError(
                401,
                "invalid_password_code",
                "인증번호가 올바르지 않거나 만료되었습니다.",
            )
        if not hmac.compare_digest(row.code_digest, proof(email, purpose, code)):
            db.execute(
                update(PasswordChallengeRow)
                .where(
                    PasswordChallengeRow.email == email,
                    PasswordChallengeRow.purpose == purpose,
                )
                .values(attempts=PasswordChallengeRow.attempts + 1)
            )
            db.commit()
            raise AppError(
                401,
                "invalid_password_code",
                "인증번호가 올바르지 않거나 만료되었습니다.",
            )
        if (
            credential.password_hash if credential else ""
        ) != row.expected_password_hash:
            raise AppError(
                409,
                "password_changed",
                "비밀번호가 변경되었습니다. 인증번호를 다시 요청하세요.",
            )
        consumed = db.execute(
            delete(PasswordChallengeRow).where(
                PasswordChallengeRow.email == email,
                PasswordChallengeRow.purpose == purpose,
                PasswordChallengeRow.code_digest == row.code_digest,
                PasswordChallengeRow.expires_at > at,
                PasswordChallengeRow.attempts < 5,
            )
        )
        if not consumed.rowcount:
            raise AppError(401, "invalid_password_code", "이미 사용한 인증번호입니다.")
        if credential:
            credential.password_hash, credential.updated_at = encoded, at
        else:
            db.add(
                AccountPasswordRow(email=email, password_hash=encoded, updated_at=at)
            )
        db.execute(
            delete(PasswordChallengeRow).where(PasswordChallengeRow.email == email)
        )
        store._revoke_active_login_sessions_for_email(db, email)
        from app.orm_models import TrustedEnvironmentRow, SecurityChallengeRow
        db.execute(delete(TrustedEnvironmentRow).where(TrustedEnvironmentRow.email == email))
        db.execute(delete(SecurityChallengeRow).where(SecurityChallengeRow.email == email))
        db.commit()
    return {"changed": True, "sessions_revoked": True}


def login(email, password, force_new_session, client_ip, device_token=None):
    email = email.lower()
    consume("pwd-login-ip", client_ip, 200, 300)
    consume("pwd-login-email", email, 10, 300)
    with store._session() as db:
        credential = db.get(AccountPasswordRow, email)
        expected = credential.password_hash if credential else ""
    if not verify_password(password, expected):
        return None
    with store._session() as db:
        # A concurrent password reset must invalidate an earlier password proof.
        credential = db.scalar(
            select(AccountPasswordRow)
            .where(AccountPasswordRow.email == email)
            .with_for_update()
        )
        if not credential or credential.password_hash != expected:
            return None
        profile = store._general_profile(db, email, issue_operator_session=False)
        if not profile:
            return None
        from app.services.login_security import is_trusted
        if not is_trusted(db, email, device_token, client_ip):
            raise AppError(403, "additional_verification_required", "새로운 브라우저 또는 IP입니다. 이메일 인증이나 패스키로 로그인해 주세요.")
        conflict = store._active_login_session_summary(db, email)
        if conflict and not force_new_session:
            raise SessionConflictError(conflict)
        if conflict:
            store._revoke_active_login_sessions_for_email(db, email)
        return store._issue_general_session(db, email, profile)
