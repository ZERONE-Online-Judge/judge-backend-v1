"""WebAuthn ceremonies. All cryptographic checks use py_webauthn."""

import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from sqlalchemy import delete, select, update, func
from sqlalchemy.exc import IntegrityError
from webauthn import (
    generate_registration_options,
    generate_authentication_options,
    options_to_json,
    verify_registration_response,
    verify_authentication_response,
    base64url_to_bytes,
)
from webauthn.helpers import bytes_to_base64url, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from app.database import SessionLocal
from app.orm_models import (
    IdentityAccountRow,
    PasskeyRow,
    SecurityChallengeRow,
    TrustedEnvironmentRow,
)
from app.services import login_security
from app.services.auth_rate_limit import consume
from app.services.errors import AppError
from app.services.security import token_hash
from app.settings import settings


def relying_party():
    parsed = urlsplit(settings.public_base_url)
    if not parsed.hostname or (
        parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}
    ):
        raise AppError(503, "passkeys_unavailable", "패스키 설정을 확인해야 합니다.")
    return parsed.hostname, f"{parsed.scheme}://{parsed.netloc}"


def invalid():
    return AppError(
        400,
        "invalid_passkey",
        "패스키 인증이 만료되었거나 확인되지 않았습니다. 다시 시도해 주세요.",
    )


def new_challenge(db, purpose, challenge, email=None, session_id=None):
    at = int(time.time())
    db.execute(
        delete(SecurityChallengeRow).where(SecurityChallengeRow.expires_at <= at)
    )
    token = secrets.token_urlsafe(32)
    db.add(
        SecurityChallengeRow(
            token_hash=token_hash(token),
            purpose=purpose,
            challenge=challenge,
            email=email,
            session_id=session_id,
            expires_at=at + 300,
        )
    )
    db.commit()
    return token


def take_challenge(db, token, purpose, session_id=None):
    row = db.scalar(
        select(SecurityChallengeRow)
        .where(SecurityChallengeRow.token_hash == token_hash(token))
        .with_for_update()
    )
    if (
        not row
        or row.purpose != purpose
        or row.session_id != session_id
        or row.expires_at <= int(time.time())
    ):
        raise invalid()
    consumed = db.execute(
        delete(SecurityChallengeRow).where(
            SecurityChallengeRow.token_hash == row.token_hash,
            SecurityChallengeRow.expires_at > int(time.time()),
        )
    )
    if not consumed.rowcount:
        raise invalid()
    # Even malformed or failed signatures consume this attempt across workers.
    db.commit()
    return row


def account_for(db, session):
    from app.services.presentation_access import is_presentation_email

    if is_presentation_email(session.email):
        raise invalid()
    account = db.scalar(
        select(IdentityAccountRow)
        .where(IdentityAccountRow.email == session.email.lower())
        .with_for_update()
    )
    if not account:
        raise invalid()
    return account


def list_passkeys(token):
    with SessionLocal() as db:
        session, recent = login_security.session_context(db, token)
        account = account_for(db, session)
        rows = db.scalars(
            select(PasskeyRow)
            .where(PasskeyRow.account_id == account.account_id)
            .order_by(PasskeyRow.created_at)
        ).all()
        return {
            "recently_verified": recent,
            "passkeys": [
                {
                    "id": row.credential_id,
                    "name": row.name,
                    "created_at": row.created_at,
                    "last_used_at": row.last_used_at,
                }
                for row in rows
            ],
        }


def registration_options(token, client_ip):
    consume("passkey-register-ip", client_ip, 600, 300)
    rp, _ = relying_party()
    with SessionLocal() as db:
        session, _ = login_security.session_context(db, token, fresh=True)
        account = account_for(db, session)
        consume("passkey-register-account", account.account_id, 20, 300)
        rows = db.scalars(
            select(PasskeyRow).where(PasskeyRow.account_id == account.account_id)
        ).all()
        if len(rows) >= 10:
            raise AppError(
                409, "passkey_limit", "패스키는 계정당 10개까지 등록할 수 있습니다."
            )
        options = generate_registration_options(
            rp_id=rp,
            rp_name="ZOJ",
            user_id=account.account_id.encode(),
            user_name=account.email,
            user_display_name=account.display_name,
            timeout=120000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row.credential_id))
                for row in rows
            ],
        )
        challenge_id = new_challenge(
            db,
            "register",
            bytes_to_base64url(options.challenge),
            account.email,
            session.general_session_id,
        )
        return {
            "challenge_id": challenge_id,
            "options": json.loads(options_to_json(options)),
        }


def register(token, challenge_id, credential, name):
    rp, origin = relying_party()
    with SessionLocal() as db:
        session, _ = login_security.session_context(db, token, fresh=True)
        challenge = take_challenge(
            db, challenge_id, "register", session.general_session_id
        )
        try:
            if parse_client_data_json(
                base64url_to_bytes(credential["response"]["clientDataJSON"])
            ).cross_origin:
                raise invalid()
            result = verify_registration_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge.challenge),
                expected_rp_id=rp,
                expected_origin=origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError):
            raise invalid()
        # Recheck live session and account after consuming/verifying the ceremony.
        session, _ = login_security.session_context(db, token, fresh=True)
        account = account_for(db, session)
        if account.email != challenge.email:
            raise invalid()
        count = db.scalar(
            select(func.count())
            .select_from(PasskeyRow)
            .where(PasskeyRow.account_id == account.account_id)
        )
        if count >= 10:
            raise AppError(
                409, "passkey_limit", "패스키는 계정당 10개까지 등록할 수 있습니다."
            )
        db.add(
            PasskeyRow(
                credential_id=bytes_to_base64url(result.credential_id),
                account_id=account.account_id,
                public_key=bytes_to_base64url(result.credential_public_key),
                sign_count=result.sign_count,
                name=name.strip() or "내 패스키",
                transports=credential.get("response", {}).get("transports", []),
                backed_up=result.credential_backed_up,
                created_at=int(time.time()),
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise AppError(409, "passkey_exists", "이미 등록한 패스키입니다.")
    notify(account.email, "등록")
    return {"registered": True}


def authentication_options(
    client_ip, email=None, *, purpose="authenticate", session_id=None, context=None
):
    # Contest participants commonly share a campus/NAT IP.
    consume("passkey-login-ip", client_ip, 1200, 300)
    rp, _ = relying_party()
    with SessionLocal() as db:
        allowed = None
        if email is not None:
            email = email.strip().lower()
            consume("passkey-login-email", email, 20, 300)
            rows = db.scalars(
                select(PasskeyRow)
                .join(
                    IdentityAccountRow,
                    PasskeyRow.account_id == IdentityAccountRow.account_id,
                )
                .where(IdentityAccountRow.email == email)
            ).all()
            if not rows:
                raise AppError(
                    400,
                    "passkey_unavailable",
                    "등록된 패스키가 없습니다. 이메일 인증으로 로그인해 주세요.",
                )
            allowed = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row.credential_id))
                for row in rows
            ]
        options = generate_authentication_options(
            rp_id=rp,
            timeout=120000,
            user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=allowed,
        )
        challenge_value = bytes_to_base64url(options.challenge)
        if context is not None:
            challenge_value = json.dumps({**context, "webauthn": challenge_value})
        challenge_id = new_challenge(
            db, purpose, challenge_value, email=email, session_id=session_id
        )
    return {
        "challenge_id": challenge_id,
        "options": json.loads(options_to_json(options)),
    }


def verify_assertion(db, challenge, credential, expected_challenge=None):
    rp, origin = relying_party()
    candidate = db.get(PasskeyRow, credential.get("id"))
    if not candidate:
        raise invalid()
    account = db.scalar(
        select(IdentityAccountRow)
        .where(IdentityAccountRow.account_id == candidate.account_id)
        .with_for_update()
    )
    row = db.scalar(
        select(PasskeyRow)
        .where(PasskeyRow.credential_id == candidate.credential_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not row:
        raise invalid()
    account = db.get(IdentityAccountRow, row.account_id)
    try:
        if parse_client_data_json(
            base64url_to_bytes(credential["response"]["clientDataJSON"])
        ).cross_origin:
            raise invalid()
        user_handle = credential.get("response", {}).get("userHandle")
        if (
            not account
            or (challenge.email is not None and account.email != challenge.email)
            or (challenge.email is None and not user_handle)
            or (
                user_handle is not None
                and base64url_to_bytes(user_handle) != account.account_id.encode()
            )
        ):
            raise invalid()
        result = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(
                expected_challenge or challenge.challenge
            ),
            expected_rp_id=rp,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(row.public_key),
            credential_current_sign_count=row.sign_count,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, TypeError, KeyError):
        raise invalid()
    row.sign_count, row.backed_up, row.last_used_at = (
        result.new_sign_count,
        result.credential_backed_up,
        int(time.time()),
    )
    return account, row


def authenticate(challenge_id, credential, force_new_session, client_ip):
    from app.services.store import store, SessionConflictError

    consume("passkey-verify-ip", client_ip, 1200, 300)
    with SessionLocal() as db:
        challenge = take_challenge(db, challenge_id, "authenticate")
        account, row = verify_assertion(db, challenge, credential)
        email = account.email
        # Persist counters even if the user declines replacing an existing session.
        db.commit()
        account = db.scalar(
            select(IdentityAccountRow)
            .where(IdentityAccountRow.account_id == row.account_id)
            .with_for_update()
        )
        # Deletion or an email change while verifying must invalidate this proof.
        live = db.scalar(
            select(PasskeyRow)
            .where(PasskeyRow.credential_id == row.credential_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not account or account.email != email or not live:
            raise invalid()
        profile = store._general_profile(db, email, issue_operator_session=False)
        if not profile:
            raise invalid()
        conflict = store._active_login_session_summary(db, email)
        if conflict and not force_new_session:
            transfer_token = new_challenge(
                db,
                "login-transfer",
                json.dumps(
                    {
                        "account_id": account.account_id,
                        "credential_id": live.credential_id,
                        "ip_hash": token_hash(client_ip),
                        "sessions": active_session_ids(db, email),
                    }
                ),
                email=email,
            )
            raise SessionConflictError({**conflict, "transfer_token": transfer_token})
        if conflict:
            store._revoke_active_login_sessions_for_email(db, email)
        return store._issue_general_session(db, email, profile, strongly_verified=True)


def active_session_ids(db, email):
    from app.services.store import store

    return sorted(
        [
            "general:" + row.general_session_id
            for row in store._active_general_session_rows(db, email)
        ]
        + [
            "team:" + row.team_session_id
            for row in store._active_team_session_rows_for_email(db, email)
        ]
    )


def confirm_login_transfer(email, proof_token, client_ip):
    """Consume a verified assertion's approval atomically with session issuance."""
    from app.services.store import store

    email = email.lower()
    consume("passkey-transfer-ip", client_ip, 1200, 300)
    with SessionLocal() as db:
        # Use the same account-first lock order as key removal/password changes.
        account = db.scalar(
            select(IdentityAccountRow)
            .where(IdentityAccountRow.email == email)
            .with_for_update()
        )
        proof = db.scalar(
            select(SecurityChallengeRow)
            .where(SecurityChallengeRow.token_hash == token_hash(proof_token))
            .with_for_update()
        )
        at = int(time.time())
        if (
            not account
            or not proof
            or proof.purpose != "login-transfer"
            or proof.email != email
            or proof.session_id is not None
            or proof.expires_at <= at
        ):
            raise invalid()
        context = json.loads(proof.challenge)
        key = db.scalar(
            select(PasskeyRow)
            .where(PasskeyRow.credential_id == context["credential_id"])
            .with_for_update()
        )
        if (
            context["account_id"] != account.account_id
            or not key
            or key.account_id != account.account_id
            or not hmac.compare_digest(context["ip_hash"], token_hash(client_ip))
        ):
            raise invalid()
        # Logging out/expiry is harmless; a newly issued session needs new consent.
        if not set(active_session_ids(db, email)).issubset(context["sessions"]):
            db.delete(proof)
            db.commit()
            raise AppError(
                409,
                "session_changed",
                "로그인 상태가 변경되었습니다. 다시 로그인해 주세요.",
            )
        profile = store._general_profile(db, email, issue_operator_session=False)
        if not profile:
            raise invalid()
        consumed = db.execute(
            delete(SecurityChallengeRow).where(
                SecurityChallengeRow.token_hash == proof.token_hash,
                SecurityChallengeRow.expires_at > int(time.time()),
            )
        )
        if not consumed.rowcount:
            raise invalid()
        store._revoke_active_login_sessions_for_email(db, email)
        return store._issue_general_session(db, email, profile, strongly_verified=True)


def remove(token, credential_id):
    with SessionLocal() as db:
        session, _ = login_security.session_context(db, token, fresh=True)
        account = account_for(db, session)
        row = db.scalar(
            select(PasskeyRow)
            .where(
                PasskeyRow.credential_id == credential_id,
                PasskeyRow.account_id == account.account_id,
            )
            .with_for_update()
        )
        if not row:
            raise AppError(404, "not_found", "패스키를 찾을 수 없습니다.")
        db.delete(row)
        db.execute(
            delete(SecurityChallengeRow).where(
                SecurityChallengeRow.email == account.email,
                SecurityChallengeRow.purpose.in_(
                    [
                        "pwd-set",
                        "pwd-reset",
                        "pwd-set-proof",
                        "pwd-reset-proof",
                        "login-transfer",
                    ]
                ),
            )
        )
        db.execute(
            delete(TrustedEnvironmentRow).where(
                TrustedEnvironmentRow.email == account.email
            )
        )
        db.commit()
    notify(account.email, "삭제")
    return {"deleted": True}


def notify(email, action):
    from app.services.store import store
    from app.services.mail_templates import render_branded_email

    body = [
        f"계정의 패스키가 {action}되었습니다.",
        "본인이 요청하지 않았다면 이메일로 로그인하여 패스키를 확인하고 비밀번호를 재설정해 주세요.",
    ]
    store.enqueue_mail(
        "account_security",
        email,
        f"[ZOJ] 패스키 {action} 알림",
        "\n".join(body),
        render_branded_email(
            title=f"패스키 {action} 알림", preheader="계정 보안 알림", body=body
        ),
    )


def request_email_verification(token, client_ip):
    from app.services.password_auth import proof
    from app.services.mail_templates import render_branded_email
    from app.services.store import store

    consume("security-mail-ip", client_ip, 1000, 3600)
    with SessionLocal() as db:
        session, _ = login_security.session_context(db, token)
        consume("security-mail-email", session.email, 10, 3600)
        consume("security-mail-cooldown", session.email, 1, 30)
        code = f"{secrets.randbelow(1_000_000):06d}"
        db.execute(
            delete(SecurityChallengeRow).where(
                SecurityChallengeRow.session_id == session.general_session_id,
                SecurityChallengeRow.purpose == "email",
            )
        )
        challenge_id = new_challenge(
            db,
            "email",
            proof(session.email, "passkeys", code),
            session.email,
            session.general_session_id,
        )
    store.enqueue_mail(
        "account_security_otp",
        session.email,
        "[ZOJ] 패스키 관리 인증번호",
        f"인증번호: {code}\n5분 동안 사용할 수 있습니다.",
        render_branded_email(
            title="패스키 관리 인증번호",
            preheader="본인 인증",
            body=["패스키를 등록하거나 삭제하려면 아래 인증번호를 입력하세요."],
            verification_code=code,
            verification_hint="5분 동안 한 번 사용할 수 있습니다.",
        ),
    )
    return {"challenge_id": challenge_id, "cooldown_seconds": 30}


def verify_email(token, challenge_id, code, client_ip):
    from app.services.password_auth import proof

    consume("security-verify-ip", client_ip, 1200, 300)
    with SessionLocal() as db:
        session, _ = login_security.session_context(db, token)
        row = db.scalar(
            select(SecurityChallengeRow)
            .where(SecurityChallengeRow.token_hash == token_hash(challenge_id))
            .with_for_update()
        )
        if (
            not row
            or row.purpose != "email"
            or row.session_id != session.general_session_id
            or row.email != session.email
            or row.expires_at <= int(time.time())
            or row.attempts >= 5
        ):
            raise invalid()
        if not hmac.compare_digest(
            row.challenge, proof(session.email, "passkeys", code)
        ):
            db.execute(
                update(SecurityChallengeRow)
                .where(SecurityChallengeRow.token_hash == row.token_hash)
                .values(attempts=SecurityChallengeRow.attempts + 1)
            )
            db.commit()
            raise AppError(
                400,
                "invalid_verification_code",
                "인증번호가 올바르지 않거나 만료되었습니다.",
            )
        consumed = db.execute(
            delete(SecurityChallengeRow).where(
                SecurityChallengeRow.token_hash == row.token_hash,
                SecurityChallengeRow.attempts < 5,
            )
        )
        if not consumed.rowcount:
            raise invalid()
        login_security.mark_verified(db, session.general_session_id)
        db.commit()
    return {"verified": True}
