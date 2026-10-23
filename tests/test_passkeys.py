"""Exercise real ES256 WebAuthn signatures without replacing the verifier."""

import hashlib
import json
import os
import re
import secrets
import time
from uuid import uuid4

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from webauthn.helpers import bytes_to_base64url as b64
from webauthn import base64url_to_bytes as unb64

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")
from app.database import SessionLocal
from app.main import app
from app.orm_models import (
    IdentityAccountRow,
    PasskeyRow,
    SessionVerificationRow,
    SecurityChallengeRow,
    TrustedEnvironmentRow,
    AccountPasswordRow,
    MailQueueItemRow,
    GeneralSessionRow,
    AuthRateLimitRow,
)
from app.services.store import store
from app.services.security import token_hash
from app.services.account_passwords import hash_password
from app.settings import settings

PREFIX = "/api/auth/general"
PASSWORD = "ZojPasskey1!"


@pytest.fixture
def browser(monkeypatch):
    monkeypatch.setattr(settings, "allow_empty_otp", False)
    monkeypatch.setattr(settings, "public_base_url", "https://zoj.kr")
    with SessionLocal() as db:
        db.execute(delete(AuthRateLimitRow))
        db.commit()
    with TestClient(app, base_url="https://zoj.kr", client=("203.0.113.7", 50000)) as c:
        yield c


def login(c, email=None):
    c.cookies.clear()
    email = email or f"passkey-{uuid4().hex}@example.com"
    with SessionLocal() as db:
        if not db.scalar(
            select(IdentityAccountRow).where(IdentityAccountRow.email == email)
        ):
            db.add(IdentityAccountRow(email=email, display_name="패스키 사용자"))
            db.commit()
    code = store.create_general_otp(email)
    response = c.post(
        PREFIX + "/otp/verify",
        json={"email": email, "otp_code": code, "force_new_session": True},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return email, {"Authorization": "Bearer " + data["access_token"]}, data


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(32)
        self.user = None

    def register(
        self, opts, origin="https://zoj.kr", flags=0x45, rp="zoj.kr", cross_origin=False
    ):
        self.user = opts["user"]["id"]
        public = self.key.public_key().public_numbers()
        cose = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: public.x.to_bytes(32, "big"),
                -3: public.y.to_bytes(32, "big"),
            }
        )
        auth = (
            hashlib.sha256(rp.encode()).digest()
            + bytes([flags])
            + bytes(4)
            + bytes(16)
            + len(self.id).to_bytes(2, "big")
            + self.id
            + cose
        )
        data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": opts["challenge"],
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode()
        return {
            "id": b64(self.id),
            "rawId": b64(self.id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64(data),
                "attestationObject": b64(
                    cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
                ),
                "transports": ["internal"],
            },
        }

    def assertion(
        self,
        opts,
        origin="https://zoj.kr",
        flags=5,
        rp="zoj.kr",
        count=1,
        cross_origin=False,
    ):
        auth = (
            hashlib.sha256(rp.encode()).digest()
            + bytes([flags])
            + count.to_bytes(4, "big")
        )
        data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": opts["challenge"],
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode()
        signature = self.key.sign(
            auth + hashlib.sha256(data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64(self.id),
            "rawId": b64(self.id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64(data),
                "authenticatorData": b64(auth),
                "signature": b64(signature),
                "userHandle": self.user,
            },
        }


def enroll(c, headers, auth=None):
    auth = auth or Authenticator()
    response = c.post(PREFIX + "/passkeys/register/options", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["options"]["authenticatorSelection"]["residentKey"] == "required"
    response = c.post(
        PREFIX + "/passkeys/register/verify",
        headers=headers,
        json={
            "challenge_id": data["challenge_id"],
            "credential": auth.register(data["options"]),
            "name": "내 노트북",
        },
    )
    assert response.status_code == 200, response.text
    return auth


def assertion(c, auth, **kwargs):
    response = c.post(PREFIX + "/passkeys/login/options")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["options"]["allowCredentials"] == []
    return {
        "challenge_id": data["challenge_id"],
        "credential": auth.assertion(data["options"], **kwargs),
    }


def test_real_registration_login_conflict_replay_and_deletion(browser):
    c = browser
    email, headers, session = login(c)
    auth = enroll(c, headers)
    listing = c.get(PREFIX + "/passkeys", headers=headers).json()["data"]
    assert listing["recently_verified"] is True and len(listing["passkeys"]) == 1
    assert set(listing["passkeys"][0]) == {"id", "name", "created_at", "last_used_at"}
    payload = assertion(c, auth)
    conflict = c.post(PREFIX + "/passkeys/login/verify", json=payload)
    assert conflict.status_code == 409
    assert (
        c.post(
            PREFIX + "/passkeys/login/verify",
            json={**payload, "force_new_session": True},
        ).status_code
        == 401
    )
    payload = assertion(c, auth, count=2)
    result = c.post(
        PREFIX + "/passkeys/login/verify", json={**payload, "force_new_session": True}
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["account"]["email"] == email
    assert c.get(PREFIX + "/me", headers=headers).status_code == 401
    headers = {"Authorization": "Bearer " + result.json()["data"]["access_token"]}
    assert (
        c.post(
            PREFIX + "/passkeys/remove",
            headers=headers,
            json={"credential_id": b64(auth.id)},
        ).status_code
        == 200
    )
    assert (
        c.post(
            PREFIX + "/passkeys/login/verify", json=assertion(c, auth, count=3)
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        assert db.get(PasskeyRow, b64(auth.id)) is None
        assert not db.scalar(
            select(TrustedEnvironmentRow).where(TrustedEnvironmentRow.email == email)
        )


@pytest.mark.parametrize(
    "kwargs", [{"origin": "https://evil.example"}, {"rp": "evil.example"}, {"flags": 1}]
)
def test_registration_rejects_wrong_origin_rp_and_missing_uv(browser, kwargs):
    _, headers, _ = login(browser)
    data = browser.post(PREFIX + "/passkeys/register/options", headers=headers).json()[
        "data"
    ]
    payload = {
        "challenge_id": data["challenge_id"],
        "credential": Authenticator().register(data["options"], **kwargs),
    }
    assert (
        browser.post(
            PREFIX + "/passkeys/register/verify", headers=headers, json=payload
        ).status_code
        == 400
    )
    assert (
        browser.get(PREFIX + "/passkeys", headers=headers).json()["data"]["passkeys"]
        == []
    )


@pytest.mark.parametrize(
    "kind",
    [
        "origin",
        "rp",
        "uv",
        "signature",
        "handle",
        "missing-handle",
        "challenge",
        "counter",
        "unknown",
        "cross-origin",
    ],
)
def test_assertion_rejects_tampering(browser, kind):
    _, headers, _ = login(browser)
    auth = enroll(browser, headers)
    kwargs = (
        {"origin": "https://evil.example"}
        if kind == "origin"
        else (
            {"rp": "evil.example"}
            if kind == "rp"
            else {"flags": 1} if kind == "uv" else {}
        )
    )
    if kind == "cross-origin":
        kwargs = {"cross_origin": True}
    payload = assertion(browser, auth, **kwargs)
    if kind == "signature":
        payload["credential"]["response"]["signature"] = b64(b"invalid")
    if kind == "handle":
        payload["credential"]["response"]["userHandle"] = b64(b"other-account")
    if kind == "missing-handle":
        payload["credential"]["response"]["userHandle"] = None
    if kind == "unknown":
        payload["credential"]["id"] = b64(b"unknown")
    if kind == "challenge":
        decoded = json.loads(unb64(payload["credential"]["response"]["clientDataJSON"]))
        decoded["challenge"] = b64(b"wrong")
        payload["credential"]["response"]["clientDataJSON"] = b64(
            json.dumps(decoded).encode()
        )
    if kind == "counter":
        with SessionLocal() as db:
            db.get(PasskeyRow, b64(auth.id)).sign_count = 5
            db.commit()
    result = browser.post(
        PREFIX + "/passkeys/login/verify", json={**payload, "force_new_session": True}
    )
    assert result.status_code == 401, result.text
    assert browser.get(PREFIX + "/me", headers=headers).status_code == 200


def test_expired_and_cross_session_ceremonies_and_weak_sessions(browser):
    email, headers, session = login(browser)
    data = browser.post(PREFIX + "/passkeys/register/options", headers=headers).json()[
        "data"
    ]
    with SessionLocal() as db:
        db.get(SecurityChallengeRow, token_hash(data["challenge_id"])).expires_at = (
            int(time.time()) - 1
        )
        db.commit()
    payload = {
        "challenge_id": data["challenge_id"],
        "credential": Authenticator().register(data["options"]),
    }
    assert (
        browser.post(
            PREFIX + "/passkeys/register/verify", headers=headers, json=payload
        ).status_code
        == 400
    )
    data = browser.post(PREFIX + "/passkeys/register/options", headers=headers).json()[
        "data"
    ]
    _, other, _ = login(browser)
    assert (
        browser.post(
            PREFIX + "/passkeys/register/verify",
            headers=other,
            json={
                "challenge_id": data["challenge_id"],
                "credential": Authenticator().register(data["options"]),
            },
        ).status_code
        == 400
    )
    with SessionLocal() as db:
        row = db.scalar(
            select(GeneralSessionRow).where(
                GeneralSessionRow.access_token_hash
                == token_hash(session["access_token"])
            )
        )
        db.get(SessionVerificationRow, row.general_session_id).verified_at = (
            int(time.time()) - 301
        )
        db.commit()
    assert (
        browser.post(PREFIX + "/passkeys/register/options", headers=headers).status_code
        == 403
    )
    # Refreshing access does not renew strong authentication freshness.
    browser.cookies.clear()
    refreshed = browser.post(
        PREFIX + "/refresh", json={"refresh_token": session["refresh_token"]}
    ).json()["data"]
    assert (
        browser.post(
            PREFIX + "/passkeys/register/options",
            headers={"Authorization": "Bearer " + refreshed["access_token"]},
        ).status_code
        == 403
    )


def security_code(email):
    with SessionLocal() as db:
        row = db.scalar(
            select(MailQueueItemRow)
            .where(
                MailQueueItemRow.recipient_email == email,
                MailQueueItemRow.mail_type == "account_security_otp",
            )
            .order_by(MailQueueItemRow.created_at.desc())
        )
        return re.search(r"인증번호: ([0-9]{6})", row.body_text).group(1)


def test_management_reauth_single_use_attempts_and_ownership(browser):
    email, headers, session = login(browser)
    auth = enroll(browser, headers)
    _, other, _ = login(browser)
    assert (
        browser.post(
            PREFIX + "/passkeys/remove",
            headers=other,
            json={"credential_id": b64(auth.id)},
        ).status_code
        == 404
    )
    data = browser.post(PREFIX + "/security/otp/request", headers=headers).json()[
        "data"
    ]
    values = {"challenge_id": data["challenge_id"], "code": security_code(email)}
    assert (
        browser.post(
            PREFIX + "/security/otp/verify", headers=other, json=values
        ).status_code
        == 400
    )
    assert (
        browser.post(
            PREFIX + "/security/otp/verify", headers=headers, json=values
        ).status_code
        == 200
    )
    assert (
        browser.post(
            PREFIX + "/security/otp/verify", headers=headers, json=values
        ).status_code
        == 400
    )
    with SessionLocal() as db:
        db.execute(delete(AuthRateLimitRow))
        db.commit()
    data = browser.post(PREFIX + "/security/otp/request", headers=headers).json()[
        "data"
    ]
    real = security_code(email)
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(5):
        assert (
            browser.post(
                PREFIX + "/security/otp/verify",
                headers=headers,
                json={"challenge_id": data["challenge_id"], "code": wrong},
            ).status_code
            == 400
        )
    assert (
        browser.post(
            PREFIX + "/security/otp/verify",
            headers=headers,
            json={"challenge_id": data["challenge_id"], "code": real},
        ).status_code
        == 400
    )


def test_password_requires_both_verified_browser_and_ip_and_reset_clears_trust(browser):
    email, headers, session = login(browser)
    with SessionLocal() as db:
        db.add(
            AccountPasswordRow(
                email=email,
                password_hash=hash_password(PASSWORD),
                updated_at=int(time.time()),
            )
        )
        db.commit()
    values = {"email": email, "password": PASSWORD, "force_new_session": True}
    assert browser.post(PREFIX + "/password/login", json=values).status_code == 200
    # Correct password with same cookie on a new IP still cannot issue a session.
    with TestClient(
        app, base_url="https://zoj.kr", client=("203.0.113.8", 50001)
    ) as other:
        other.cookies.update(browser.cookies)
        result = other.post(
            PREFIX + "/password/login",
            json=values,
            headers={"X-Forwarded-For": "203.0.113.7", "X-Real-IP": "203.0.113.7"},
        )
        assert (
            result.status_code == 403
            and result.json()["error"]["code"] == "additional_verification_required"
        )
        assert "zoj_sso" not in result.cookies
    with TestClient(
        app, base_url="https://zoj.kr", client=("203.0.113.7", 50002)
    ) as other:
        assert other.post(PREFIX + "/password/login", json=values).status_code == 403
        assert (
            other.post(
                PREFIX + "/password/login", json={**values, "password": "wrong"}
            ).status_code
            == 401
        )
    # Password-only login never gives fresh authority to add credentials.
    session = browser.post(PREFIX + "/password/login", json=values).json()["data"]
    weak = {"Authorization": "Bearer " + session["access_token"]}
    assert (
        browser.post(PREFIX + "/passkeys/register/options", headers=weak).status_code
        == 403
    )
    from app.services.password_auth import request_change, confirm_change
    from test_account_passwords import code_for

    request_change(email, "reset", "203.0.113.7")
    confirm_change(email, "reset", code_for(email), PASSWORD + "Reset", "203.0.113.7")
    assert (
        browser.post(
            PREFIX + "/password/login", json={**values, "password": PASSWORD + "Reset"}
        ).status_code
        == 403
    )
    assert browser.get(PREFIX + "/me", headers=weak).status_code == 401


def test_routes_require_bearer_auth_and_hide_validation_payload(browser):
    assert browser.get(PREFIX + "/passkeys").status_code == 401
    assert browser.post(PREFIX + "/passkeys/register/options").status_code == 401
    assert browser.post(PREFIX + "/security/otp/request").status_code == 401
    huge = "sensitive-data-" * 3000
    result = browser.post(
        PREFIX + "/passkeys/login/verify",
        json={"challenge_id": "x" * 43, "credential": {"secret": huge}},
    )
    assert result.status_code == 422 and "sensitive-data" not in result.text


def test_synced_passkeys_with_zero_counter_remain_usable(browser):
    _, headers, _ = login(browser)
    auth = Authenticator()
    data = browser.post(PREFIX + "/passkeys/register/options", headers=headers).json()[
        "data"
    ]
    result = browser.post(
        PREFIX + "/passkeys/register/verify",
        headers=headers,
        json={
            "challenge_id": data["challenge_id"],
            "credential": auth.register(data["options"], flags=0x5D),
        },
    )
    assert result.status_code == 200, result.text
    for _ in range(2):
        result = browser.post(
            PREFIX + "/passkeys/login/verify",
            json={
                **assertion(browser, auth, flags=0x1D, count=0),
                "force_new_session": True,
            },
        )
        assert result.status_code == 200, result.text


def test_registration_replay_duplicate_and_limit(browser):
    _, headers, _ = login(browser)
    auth = Authenticator()
    data = browser.post(PREFIX + "/passkeys/register/options", headers=headers).json()[
        "data"
    ]
    values = {
        "challenge_id": data["challenge_id"],
        "credential": auth.register(data["options"]),
    }
    assert (
        browser.post(
            PREFIX + "/passkeys/register/verify", headers=headers, json=values
        ).status_code
        == 200
    )
    assert (
        browser.post(
            PREFIX + "/passkeys/register/verify", headers=headers, json=values
        ).status_code
        == 400
    )
    data = browser.post(PREFIX + "/passkeys/register/options", headers=headers).json()[
        "data"
    ]
    assert data["options"]["excludeCredentials"][0]["id"] == b64(auth.id)
    assert (
        browser.post(
            PREFIX + "/passkeys/register/verify",
            headers=headers,
            json={
                "challenge_id": data["challenge_id"],
                "credential": auth.register(data["options"]),
            },
        ).status_code
        == 409
    )
    with SessionLocal() as db:
        row = db.get(PasskeyRow, b64(auth.id))
        for n in range(9):
            db.add(
                PasskeyRow(
                    credential_id=b64(secrets.token_bytes(32)),
                    account_id=row.account_id,
                    public_key=row.public_key,
                    name="추가",
                    created_at=int(time.time()),
                )
            )
        db.commit()
    assert (
        browser.post(PREFIX + "/passkeys/register/options", headers=headers).status_code
        == 409
    )


def test_production_device_cookie_is_host_scoped_secure_and_http_only(
    browser, monkeypatch
):
    monkeypatch.setattr(settings, "app_env", "production")
    login(browser)
    cookie = next(
        item for item in browser.cookies.jar if item.name == "__Host-zoj_device"
    )
    assert cookie.secure and not cookie.domain_specified and cookie.path == "/"
    assert cookie.has_nonstandard_attr("HttpOnly")


def test_audit_ip_does_not_trust_user_supplied_headers():
    from starlette.requests import Request
    from app.services.access_logging import client_ip

    request = Request(
        {
            "type": "http",
            "client": ("203.0.113.8", 1234),
            "headers": [(b"x-forwarded-for", b"203.0.113.7")],
        }
    )
    assert client_ip(request) == "203.0.113.8"


def test_shared_campus_ip_can_start_more_than_sixty_independent_logins(browser):
    for _ in range(65):
        response = browser.post(PREFIX + "/passkeys/login/options")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["options"]["allowCredentials"] == []


def test_email_first_lookup_and_bound_passkey_login(browser):
    c = browser
    email, headers, _ = login(c)
    auth = enroll(c, headers)
    other_email, other_headers, _ = login(c)
    other_auth = enroll(c, other_headers)
    with SessionLocal() as db:
        db.add(
            AccountPasswordRow(
                email=email,
                password_hash=hash_password(PASSWORD),
                updated_at=int(time.time()),
            )
        )
        db.commit()
    methods = c.post(PREFIX + "/login-options", json={"email": email.upper()})
    assert methods.status_code == 200, methods.text
    assert methods.json()["data"] == {
        "password_available": True,
        "passkey_available": True,
    }
    response = c.post(PREFIX + "/passkeys/login/options", json={"email": email.upper()})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [row["id"] for row in data["options"]["allowCredentials"]] == [b64(auth.id)]
    assert "user" not in data["options"]
    with SessionLocal() as db:
        assert (
            db.get(SecurityChallengeRow, token_hash(data["challenge_id"])).email
            == email
        )
    # A valid signature from another registered account must not bypass binding,
    # even if a malicious client ignores the server-provided allow list.
    wrong = c.post(
        PREFIX + "/passkeys/login/verify",
        json={
            "challenge_id": data["challenge_id"],
            "credential": other_auth.assertion(data["options"]),
            "force_new_session": True,
        },
    )
    assert wrong.status_code == 401, wrong.text
    data = c.post(PREFIX + "/passkeys/login/options", json={"email": email}).json()[
        "data"
    ]
    credential = other_auth.assertion(data["options"])
    credential["response"]["userHandle"] = None
    wrong = c.post(
        PREFIX + "/passkeys/login/verify",
        json={
            "challenge_id": data["challenge_id"],
            "credential": credential,
            "force_new_session": True,
        },
    )
    assert wrong.status_code == 401, wrong.text
    data = c.post(PREFIX + "/passkeys/login/options", json={"email": email}).json()[
        "data"
    ]
    result = c.post(
        PREFIX + "/passkeys/login/verify",
        json={
            "challenge_id": data["challenge_id"],
            "credential": auth.assertion(data["options"]),
            "force_new_session": True,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["account"]["email"] == email
    assert result.json()["data"]["account"]["email"] != other_email


@pytest.mark.parametrize(
    "payload", [{}, {"email": ""}, {"email": "not-an-email"}, {"email": None}]
)
def test_bound_options_reject_invalid_email_body(browser, payload):
    assert (
        browser.post(PREFIX + "/passkeys/login/options", json=payload).status_code
        == 422
    )


def test_bound_options_do_not_fall_back_to_unrestricted_credentials(browser):
    c = browser
    email, _, _ = login(c)
    for address in [email, f"missing-{uuid4().hex}@example.com"]:
        result = c.post(PREFIX + "/passkeys/login/options", json={"email": address})
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "passkey_unavailable"


def test_bound_options_reject_a_credential_deleted_after_challenge(browser):
    c = browser
    email, headers, _ = login(c)
    auth = enroll(c, headers)
    data = c.post(PREFIX + "/passkeys/login/options", json={"email": email}).json()[
        "data"
    ]
    assert (
        c.post(
            PREFIX + "/passkeys/remove",
            headers=headers,
            json={"credential_id": b64(auth.id)},
        ).status_code
        == 200
    )
    result = c.post(
        PREFIX + "/passkeys/login/verify",
        json={
            "challenge_id": data["challenge_id"],
            "credential": auth.assertion(data["options"]),
            "force_new_session": True,
        },
    )
    assert result.status_code == 401


def test_email_first_lookup_accepts_shared_ip_but_limits_one_email(browser):
    for _ in range(65):
        response = browser.post(
            PREFIX + "/login-options", json={"email": f"{uuid4().hex}@example.com"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "password_available": False,
            "passkey_available": False,
        }
    email = f"{uuid4().hex}@example.com"
    for _ in range(10):
        assert (
            browser.post(PREFIX + "/login-options", json={"email": email}).status_code
            == 200
        )
    assert (
        browser.post(PREFIX + "/login-options", json={"email": email}).status_code
        == 429
    )


@pytest.mark.parametrize("handle", [None, "omitted", "", "wrong"])
def test_bound_login_handles_optional_user_handle_safely(browser, handle):
    c = browser
    email, headers, _ = login(c)
    auth = enroll(c, headers)
    data = c.post(PREFIX + "/passkeys/login/options", json={"email": email}).json()[
        "data"
    ]
    credential = auth.assertion(data["options"])
    if handle == "omitted":
        credential["response"].pop("userHandle")
    else:
        credential["response"]["userHandle"] = (
            b64(b"other-account") if handle == "wrong" else handle
        )
    result = c.post(
        PREFIX + "/passkeys/login/verify",
        json={
            "challenge_id": data["challenge_id"],
            "credential": credential,
            "force_new_session": True,
        },
    )
    assert result.status_code == (
        200 if handle in (None, "omitted") else 401
    ), result.text
    if result.status_code == 200:
        assert result.json()["data"]["account"]["email"] == email
