"""A single real passkey assertion authorizes an explicit session transfer."""

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from app.database import SessionLocal
from app.orm_models import (
    IdentityAccountRow,
    GeneralSessionRow,
    PasskeyRow,
    SecurityChallengeRow,
    SessionVerificationRow,
)
from app.services import passkeys
from app.services.errors import AppError
from app.services.security import token_hash
from test_passkeys import browser, login, enroll, assertion, PREFIX, b64

BASE = PREFIX + "/passkeys/login"


def prepare(c):
    email, headers, session = login(c)
    auth = enroll(c, headers)
    response = c.post(BASE + "/verify", json=assertion(c, auth))
    assert response.status_code == 409, response.text
    assert "access_token" not in response.text
    assert "set-cookie" not in response.headers
    assert c.get(PREFIX + "/me", headers=headers).status_code == 200
    proof = response.json()["error"]["details"]["transfer_token"]
    return email, headers, auth, proof


def confirm(c, email, proof):
    return c.post(BASE + "/transfer", json={"email": email, "proof_token": proof})


def test_one_assertion_transfers_session_once_and_marks_login_verified(browser):
    c = browser
    email, headers, auth, proof = prepare(c)
    with SessionLocal() as db:
        assert db.get(PasskeyRow, b64(auth.id)).sign_count == 1
        assert (
            len(
                db.scalars(
                    select(GeneralSessionRow).where(GeneralSessionRow.email == email)
                ).all()
            )
            == 1
        )
    result = confirm(c, email.upper(), proof)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["account"]["email"] == email
    assert "set-cookie" in result.headers
    assert c.get(PREFIX + "/me", headers=headers).status_code == 401
    new_headers = {"Authorization": "Bearer " + result.json()["data"]["access_token"]}
    assert c.get(PREFIX + "/me", headers=new_headers).status_code == 200
    with SessionLocal() as db:
        assert db.get(SecurityChallengeRow, token_hash(proof)) is None
        assert db.get(PasskeyRow, b64(auth.id)).sign_count == 1
        active = db.scalar(
            select(GeneralSessionRow).where(
                GeneralSessionRow.email == email, GeneralSessionRow.revoked_at.is_(None)
            )
        )
        assert db.get(SessionVerificationRow, active.general_session_id)
    assert confirm(c, email, proof).status_code == 400
    assert c.get(PREFIX + "/me", headers=new_headers).status_code == 200


@pytest.mark.parametrize(
    "change", ["expired", "purpose", "email", "account", "key", "ip"]
)
def test_transfer_rejects_invalid_or_unbound_proof_without_revoking_session(
    browser, change
):
    c = browser
    email, headers, auth, proof = prepare(c)
    with SessionLocal() as db:
        row = db.get(SecurityChallengeRow, token_hash(proof))
        context = json.loads(row.challenge)
        if change == "expired":
            row.expires_at = int(time.time()) - 1
        elif change == "purpose":
            row.purpose = "pwd-reset-proof"
        elif change == "email":
            row.email = "other@example.com"
        elif change == "account":
            context["account_id"] = "other-account"
        elif change == "key":
            db.delete(db.get(PasskeyRow, b64(auth.id)))
        elif change == "ip":
            context["ip_hash"] = token_hash("203.0.113.99")
        row.challenge = json.dumps(context)
        db.commit()
    result = confirm(c, email, proof)
    assert result.status_code == 400, result.text
    assert "access_token" not in result.text
    assert c.get(PREFIX + "/me", headers=headers).status_code == 200


def test_transfer_rejects_wrong_email_and_forged_token(browser):
    c = browser
    email, headers, _, proof = prepare(c)
    other, other_headers, _ = login(c)
    assert confirm(c, other, proof).status_code == 400
    assert confirm(c, email, "x" * 43).status_code == 400
    assert c.get(PREFIX + "/me", headers=headers).status_code == 200
    assert c.get(PREFIX + "/me", headers=other_headers).status_code == 200


def test_key_removal_invalidates_pending_transfer(browser):
    c = browser
    email, headers, auth, proof = prepare(c)
    response = c.post(
        PREFIX + "/passkeys/remove",
        headers=headers,
        json={"credential_id": b64(auth.id)},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.get(SecurityChallengeRow, token_hash(proof)) is None
    assert confirm(c, email, proof).status_code == 400
    assert c.get(PREFIX + "/me", headers=headers).status_code == 200


def test_transfer_cannot_revoke_a_session_created_after_confirmation_was_offered(
    browser,
):
    c = browser
    email, _, _, proof = prepare(c)
    _, new_headers, _ = login(c, email)
    result = confirm(c, email, proof)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "session_changed"
    assert c.get(PREFIX + "/me", headers=new_headers).status_code == 200
    assert confirm(c, email, proof).status_code == 400


def test_transfer_succeeds_if_original_session_has_logged_out(browser):
    c = browser
    email, headers, _, proof = prepare(c)
    with SessionLocal() as db:
        row = db.scalar(
            select(GeneralSessionRow).where(GeneralSessionRow.email == email)
        )
        from app.models import now_utc

        row.revoked_at = now_utc()
        db.commit()
    assert confirm(c, email, proof).status_code == 200


def test_proof_consumption_rolls_back_with_failed_session_issuance(
    browser, monkeypatch
):
    c = browser
    email, headers, _, proof = prepare(c)
    from app.services.store import store

    original = store._issue_general_session

    def fail(*args, **kwargs):
        raise RuntimeError("simulated issuance failure")

    monkeypatch.setattr(store, "_issue_general_session", fail)
    with pytest.raises(RuntimeError, match="simulated issuance failure"):
        passkeys.confirm_login_transfer(email, proof, "203.0.113.7")
    assert c.get(PREFIX + "/me", headers=headers).status_code == 200
    with SessionLocal() as db:
        assert db.get(SecurityChallengeRow, token_hash(proof))
    monkeypatch.setattr(store, "_issue_general_session", original)
    assert confirm(c, email, proof).status_code == 200


def test_email_change_invalidates_transfer(browser):
    c = browser
    email, _, _, proof = prepare(c)
    with SessionLocal() as db:
        row = db.scalar(
            select(IdentityAccountRow).where(IdentityAccountRow.email == email)
        )
        row.email = "changed-" + email
        db.commit()
    assert confirm(c, email, proof).status_code == 400


@pytest.mark.parametrize("same_proof", [True, False])
def test_concurrent_transfers_issue_only_one_session(browser, same_proof):
    if SessionLocal.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks")
    c = browser
    email, _, auth, proof = prepare(c)
    other_proof = proof
    if not same_proof:
        response = c.post(BASE + "/verify", json=assertion(c, auth, count=2))
        assert response.status_code == 409, response.text
        other_proof = response.json()["error"]["details"]["transfer_token"]
    barrier = threading.Barrier(2)

    def transfer(token):
        barrier.wait(timeout=10)
        try:
            passkeys.confirm_login_transfer(email, token, "203.0.113.7")
            return 200
        except AppError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(transfer, token) for token in (proof, other_proof)]
        results = sorted(task.result(timeout=15) for task in tasks)
    assert results == [200, 400 if same_proof else 409]
    with SessionLocal() as db:
        sessions = db.scalars(
            select(GeneralSessionRow).where(GeneralSessionRow.email == email)
        ).all()
        assert len(sessions) == 2
        assert sum(row.revoked_at is None for row in sessions) == 1
