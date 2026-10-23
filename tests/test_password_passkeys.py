"""Password changes require a real, single-use passkey assertion for that purpose."""

import time
from uuid import uuid4
import pytest
from sqlalchemy import select
from app.database import SessionLocal
from app.orm_models import (
    AccountPasswordRow,
    SecurityChallengeRow,
    PasskeyRow,
    TrustedEnvironmentRow,
    GeneralSessionRow,
)
from app.services.account_passwords import hash_password, verify_password
from app.services.security import token_hash
from test_passkeys import browser, login, enroll, assertion, PREFIX, PASSWORD

BASE = PREFIX + "/password/passkey"
NEW_PASSWORD = "ChangedPasskey2!"


def prepare(c, purpose="reset"):
    email, headers, _ = login(c)
    auth = enroll(c, headers)
    with SessionLocal() as db:
        db.add(
            AccountPasswordRow(
                email=email,
                password_hash=hash_password(PASSWORD),
                updated_at=int(time.time()),
            )
        )
        db.commit()
    scope = {"purpose": purpose, **({"email": email} if purpose == "reset" else {})}
    return email, headers, auth, scope


def begin(c, scope, headers=None):
    response = c.post(BASE + "/options", json=scope, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def prove(c, auth, scope, headers=None, count=1):
    data = begin(c, scope, headers)
    credential = auth.assertion(data["options"], count=count)
    credential["response"]["userHandle"] = None
    response = c.post(
        BASE + "/verify",
        headers=headers or {},
        json={**scope, "challenge_id": data["challenge_id"], "credential": credential},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["proof_token"]


@pytest.mark.parametrize("purpose", ["set", "reset"])
def test_passkey_password_change_revokes_sessions_and_trust_without_a_login(
    browser, purpose
):
    c = browser
    email, headers, auth, scope = prepare(c, purpose)
    with SessionLocal() as db:
        count = len(
            db.scalars(
                select(GeneralSessionRow).where(GeneralSessionRow.email == email)
            ).all()
        )
    proof = prove(c, auth, scope, headers if purpose == "set" else None)
    with SessionLocal() as db:
        assert (
            len(
                db.scalars(
                    select(GeneralSessionRow).where(GeneralSessionRow.email == email)
                ).all()
            )
            == count
        )
        assert db.get(SecurityChallengeRow, token_hash(proof)).email == email
    response = c.post(
        BASE + "/confirm",
        headers=headers if purpose == "set" else {},
        json={**scope, "proof_token": proof, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"changed": True, "sessions_revoked": True}
    assert c.get(PREFIX + "/me", headers=headers).status_code == 401
    with SessionLocal() as db:
        assert verify_password(
            NEW_PASSWORD, db.get(AccountPasswordRow, email).password_hash
        )
        assert not db.scalar(
            select(TrustedEnvironmentRow).where(TrustedEnvironmentRow.email == email)
        )
        assert not db.scalar(
            select(SecurityChallengeRow).where(SecurityChallengeRow.email == email)
        )
    replay = c.post(
        BASE + "/confirm",
        headers=headers if purpose == "set" else {},
        json={**scope, "proof_token": proof, "new_password": PASSWORD},
    )
    assert replay.status_code in (400, 401)


def test_passkey_can_register_the_first_password(browser):
    c = browser
    email, headers, _ = login(c)
    auth = enroll(c, headers)
    scope = {"purpose": "set"}
    proof = prove(c, auth, scope, headers)
    result = c.post(
        BASE + "/confirm",
        headers=headers,
        json={**scope, "proof_token": proof, "new_password": NEW_PASSWORD},
    )
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        assert verify_password(
            NEW_PASSWORD, db.get(AccountPasswordRow, email).password_hash
        )


@pytest.mark.parametrize(
    "kind",
    [
        "other-account",
        "login-challenge",
        "wrong-purpose",
        "wrong-origin",
        "missing-uv",
        "signature",
    ],
)
def test_invalid_or_cross_purpose_assertions_cannot_authorize_password_change(
    browser, kind
):
    c = browser
    email, headers, auth, scope = prepare(c)
    data = begin(c, scope)
    credential = auth.assertion(data["options"])
    if kind == "other-account":
        _, other_headers, _ = login(c)
        other_auth = enroll(c, other_headers)
        credential = other_auth.assertion(data["options"])
        credential["response"]["userHandle"] = None
    elif kind == "login-challenge":
        payload = assertion(c, auth)
        data["challenge_id"] = payload["challenge_id"]
        credential = payload["credential"]
    elif kind == "wrong-purpose":
        data = begin(c, {"purpose": "set"}, headers)
        credential = auth.assertion(data["options"])
    elif kind == "wrong-origin":
        credential = auth.assertion(data["options"], origin="https://evil.example")
    elif kind == "missing-uv":
        credential = auth.assertion(data["options"], flags=1)
    elif kind == "signature":
        credential["response"]["signature"] = "aW52YWxpZA"
    result = c.post(
        BASE + "/verify",
        json={**scope, "challenge_id": data["challenge_id"], "credential": credential},
    )
    assert result.status_code == 400, result.text
    assert "proof_token" not in result.text
    with SessionLocal() as db:
        assert verify_password(
            PASSWORD, db.get(AccountPasswordRow, email).password_hash
        )


@pytest.mark.parametrize(
    "kind",
    [
        "expired",
        "deleted-key",
        "wrong-email",
        "changed-password",
        "wrong-purpose",
        "challenge-as-proof",
    ],
)
def test_proof_cannot_outlive_its_identity_credential_or_password(browser, kind):
    c = browser
    email, headers, auth, scope = prepare(c)
    proof = prove(c, auth, scope)
    with SessionLocal() as db:
        if kind == "expired":
            db.get(SecurityChallengeRow, token_hash(proof)).expires_at = (
                int(time.time()) - 1
            )
        elif kind == "deleted-key":
            db.delete(db.get(PasskeyRow, auth.assertion({"challenge": "unused"})["id"]))
        elif kind == "changed-password":
            db.get(AccountPasswordRow, email).password_hash = hash_password(
                "AnotherPassword3!"
            )
        db.commit()
    if kind == "wrong-email":
        scope = {**scope, "email": f"other-{uuid4().hex}@example.com"}
    elif kind == "wrong-purpose":
        scope = {"purpose": "set"}
    elif kind == "challenge-as-proof":
        proof = begin(c, scope)["challenge_id"]
    result = c.post(
        BASE + "/confirm",
        headers=headers if kind == "wrong-purpose" else {},
        json={**scope, "proof_token": proof, "new_password": NEW_PASSWORD},
    )
    assert result.status_code in (400, 409), result.text
    with SessionLocal() as db:
        assert not verify_password(
            NEW_PASSWORD, db.get(AccountPasswordRow, email).password_hash
        )


def test_challenge_and_proof_expiry_replay_and_revoked_session(browser):
    c = browser
    email, headers, auth, scope = prepare(c, "set")
    data = begin(c, scope, headers)
    payload = {
        **scope,
        "challenge_id": data["challenge_id"],
        "credential": auth.assertion(data["options"]),
    }
    result = c.post(BASE + "/verify", headers=headers, json=payload)
    assert result.status_code == 200
    assert c.post(BASE + "/verify", headers=headers, json=payload).status_code == 400
    proof = result.json()["data"]["proof_token"]
    _, replacement, _ = login(c, email)
    assert (
        c.post(
            BASE + "/confirm",
            headers=headers,
            json={**scope, "proof_token": proof, "new_password": NEW_PASSWORD},
        ).status_code
        == 401
    )
    assert (
        c.post(
            BASE + "/confirm",
            headers=replacement,
            json={**scope, "proof_token": proof, "new_password": NEW_PASSWORD},
        ).status_code
        == 400
    )


def test_passkey_password_requests_validate_scope_and_do_not_expose_passwords(browser):
    c = browser
    email, headers, auth, scope = prepare(c)
    assert c.post(BASE + "/options", json={"purpose": "set"}).status_code == 401
    for payload in [
        {"purpose": "reset"},
        {"purpose": "other", "email": email},
        {"purpose": "reset", "email": "bad"},
    ]:
        assert c.post(BASE + "/options", json=payload).status_code == 422
    data = begin(c, scope)
    assert (
        c.post(
            PREFIX + "/passkeys/login/verify",
            json={
                "challenge_id": data["challenge_id"],
                "credential": auth.assertion(data["options"]),
            },
        ).status_code
        == 401
    )
    proof = prove(c, auth, scope)
    response = c.post(
        BASE + "/confirm",
        json={**scope, "proof_token": proof, "new_password": "bad-secret"},
    )
    assert response.status_code == 422 and "bad-secret" not in response.text
    assert (
        c.post(
            BASE + "/options",
            headers=headers,
            json={"purpose": "set", "email": "other@example.com"},
        ).status_code
        == 400
    )


def test_unknown_email_options_are_rate_limited(browser):
    scope = {"purpose": "reset", "email": f"missing-{uuid4().hex}@example.com"}
    for _ in range(20):
        assert browser.post(BASE + "/options", json=scope).status_code == 400
    assert browser.post(BASE + "/options", json=scope).status_code == 429


def test_password_change_during_a_pending_ceremony_invalidates_the_assertion(browser):
    c = browser
    email, _, auth, scope = prepare(c)
    data = begin(c, scope)
    with SessionLocal() as db:
        db.get(AccountPasswordRow, email).password_hash = hash_password(
            "AnotherPassword3!"
        )
        db.commit()
    result = c.post(
        BASE + "/verify",
        json={
            **scope,
            "challenge_id": data["challenge_id"],
            "credential": auth.assertion(data["options"]),
        },
    )
    assert result.status_code == 400
    assert "proof_token" not in result.text


def test_expired_password_ceremony_rejects_a_valid_signature(browser):
    c = browser
    _, _, auth, scope = prepare(c)
    data = begin(c, scope)
    with SessionLocal() as db:
        db.get(SecurityChallengeRow, token_hash(data["challenge_id"])).expires_at = (
            int(time.time()) - 1
        )
        db.commit()
    result = c.post(
        BASE + "/verify",
        json={
            **scope,
            "challenge_id": data["challenge_id"],
            "credential": auth.assertion(data["options"]),
        },
    )
    assert result.status_code == 400


def test_removing_a_passkey_revokes_pending_password_proofs(browser):
    c = browser
    _, headers, auth, scope = prepare(c)
    proof = prove(c, auth, scope)
    from webauthn.helpers import bytes_to_base64url

    response = c.post(
        PREFIX + "/passkeys/remove",
        headers=headers,
        json={"credential_id": bytes_to_base64url(auth.id)},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.get(SecurityChallengeRow, token_hash(proof)) is None
    assert (
        c.post(
            BASE + "/confirm",
            json={**scope, "proof_token": proof, "new_password": NEW_PASSWORD},
        ).status_code
        == 400
    )
