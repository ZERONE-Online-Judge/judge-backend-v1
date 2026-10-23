"""Single-use, purpose-bound passkey authorization for password changes."""

import json
import time
from sqlalchemy import delete, select
from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import (
    AccountPasswordRow,
    GeneralSessionRow,
    PasskeyRow,
    SecurityChallengeRow,
)
from app.services import login_security, passkeys, password_auth
from app.services.auth_rate_limit import consume
from app.services.errors import AppError
from app.services.security import token_hash
from app.services.store import store, _aware


def identity(token, email, purpose):
    if purpose == "set":
        with SessionLocal() as db:
            session, _ = login_security.session_context(db, token)
            if email is not None and email.lower() != session.email.lower():
                raise passkeys.invalid()
            return session.email.lower(), session.general_session_id
    if purpose != "reset" or not email:
        raise passkeys.invalid()
    return email.lower(), None


def check_session(db, email, session_id):
    if session_id is None:
        return
    session = db.scalar(
        select(GeneralSessionRow)
        .where(GeneralSessionRow.general_session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not session
        or session.email.lower() != email
        or session.revoked_at
        or _aware(session.access_expires_at) <= now_utc()
    ):
        raise AppError(401, "authentication_required", "다시 로그인해 주세요.")


def options(token, email, purpose, client_ip):
    consume("pwd-passkey-options-ip", client_ip, 1200, 300)
    email, session_id = identity(token, email, purpose)
    consume("pwd-passkey-options-email", email, 20, 300)
    with SessionLocal() as db:
        credential = db.get(AccountPasswordRow, email)
        if not store._has_general_login_identity(db, email) or (
            purpose == "reset" and not credential
        ):
            raise AppError(
                400,
                "passkey_unavailable",
                "패스키로 인증할 수 없습니다. 이메일 인증을 이용해 주세요.",
            )
        expected = credential.password_hash if credential else ""
    return passkeys.authentication_options(
        client_ip,
        email,
        purpose="pwd-" + purpose,
        session_id=session_id,
        context={"password_hash": expected},
    )


def verify(token, email, purpose, challenge_id, credential, client_ip):
    email, session_id = identity(token, email, purpose)
    consume("pwd-passkey-verify-ip", client_ip, 1200, 300)
    with SessionLocal() as db:
        challenge = passkeys.take_challenge(
            db, challenge_id, "pwd-" + purpose, session_id
        )
        if challenge.email != email:
            raise passkeys.invalid()
        context = json.loads(challenge.challenge)
        account, key = passkeys.verify_assertion(
            db, challenge, credential, context["webauthn"]
        )
        check_session(db, email, session_id)
        password = db.scalar(
            select(AccountPasswordRow)
            .where(AccountPasswordRow.email == email)
            .with_for_update()
        )
        expected = password.password_hash if password else ""
        if (
            account.email != email
            or expected != context["password_hash"]
            or not store._has_general_login_identity(db, email)
        ):
            raise passkeys.invalid()
        proof = passkeys.new_challenge(
            db,
            "pwd-" + purpose + "-proof",
            json.dumps({"password_hash": expected, "credential_id": key.credential_id}),
            email=email,
            session_id=session_id,
        )
    return {"proof_token": proof, "valid_seconds": 300}


def consume_proof(db, email, purpose, proof_token, session_id, password):
    row = db.scalar(
        select(SecurityChallengeRow)
        .where(SecurityChallengeRow.token_hash == token_hash(proof_token))
        .with_for_update()
    )
    at = int(time.time())
    if (
        not row
        or row.purpose != "pwd-" + purpose + "-proof"
        or row.email != email
        or row.session_id != session_id
        or row.expires_at <= at
    ):
        raise passkeys.invalid()
    context = json.loads(row.challenge)
    if context["password_hash"] != (password.password_hash if password else ""):
        raise AppError(
            409, "password_changed", "비밀번호가 변경되었습니다. 다시 인증해 주세요."
        )
    key = db.scalar(
        select(PasskeyRow)
        .where(PasskeyRow.credential_id == context["credential_id"])
        .with_for_update()
    )
    from app.orm_models import IdentityAccountRow

    account = db.get(IdentityAccountRow, key.account_id) if key else None
    if (
        not account
        or account.email != email
        or not store._has_general_login_identity(db, email)
    ):
        raise passkeys.invalid()
    check_session(db, email, session_id)
    consumed = db.execute(
        delete(SecurityChallengeRow).where(
            SecurityChallengeRow.token_hash == row.token_hash,
            SecurityChallengeRow.expires_at > at,
        )
    )
    if not consumed.rowcount:
        raise passkeys.invalid()


def confirm(token, email, purpose, proof_token, new_password, client_ip):
    email, session_id = identity(token, email, purpose)
    result = password_auth.confirm_change(
        email,
        purpose,
        "",
        new_password,
        client_ip,
        passkey_proof=proof_token,
        session_id=session_id,
    )
    return email, result
