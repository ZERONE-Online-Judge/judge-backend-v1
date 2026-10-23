import re
import time
import hashlib
import pytest
from uuid import uuid4
from sqlalchemy import delete, select
from app.database import SessionLocal
from app.orm_models import (
    AccountPasswordRow,
    PasswordChallengeRow,
    AuthRateLimitRow,
    MailQueueItemRow,
)
from app.services.account_passwords import verify_password, validate_password, hash_password, ROUNDS
from app.services.store import store
from test_problem_archives import context
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app, client=("203.0.113.20", 50000))
from test_editor_bridge import email_for

PASSWORD = "ZojTes1!"


@pytest.mark.parametrize('value', ['abcdef1!', 'ABCDEF1!', 'aB12345_', 'Zoj1234@', 'A1!' + 'a' * 125])
def test_new_password_policy_accepts_each_case_and_ascii_symbols(value):
    assert validate_password(value) == value


@pytest.mark.parametrize('value', ['abcde1!', 'abcdef12', 'abcdefg!', '1234567!', '한글abcd1!', 'abcd 12!', 'abcd\t12!', 'abcd１２!', 'abcd12！', 'A1!' + 'a' * 126])
def test_new_password_policy_rejects_missing_conditions(value, context):
    with pytest.raises(ValueError):
        validate_password(value)
    for path in ['/api/auth/general/password/confirm', '/api/auth/general/password/reset/confirm']:
        response = client.post(path, headers=context['owner'], json={'otp_code': '123456', 'new_password': value, **({'email': email_for(context)} if '/reset/' in path else {})})
        assert response.status_code == 422
        assert value not in response.text
    from app.routers.editor_auth import Call
    with pytest.raises(ValueError):
        Call(new_password=value)


def test_password_comparison_is_case_sensitive_and_keeps_legacy_credentials():
    encoded = hash_password('Abcdef1!')
    assert verify_password('Abcdef1!', encoded)
    assert not verify_password('abcdef1!', encoded)
    legacy = '우리 서비스의 기존 비밀번호 2026'
    salt = '1' * 32
    digest = hashlib.pbkdf2_hmac('sha256', legacy.encode(), bytes.fromhex(salt), ROUNDS).hex()
    old_hash = f'pbkdf2-sha256-v1${ROUNDS}${salt}${digest}'
    assert verify_password(legacy, old_hash)


def test_login_options_only_returns_the_password_method(context):
    email = email_for(context)
    result = client.post("/api/auth/general/login-options", json={"email": email})
    assert result.status_code == 200 and result.json()["data"] == {
        "password_available": False
    }
    register(context)
    assert client.post("/api/auth/general/login-options", json={"email": email}).json()[
        "data"
    ] == {"password_available": True}
    assert client.post(
        "/api/auth/general/login-options", json={"email": uuid4().hex + "@example.com"}
    ).json()["data"] == {"password_available": False}


def code_for(email):
    with SessionLocal() as db:
        row = db.scalar(
            select(MailQueueItemRow)
            .where(
                MailQueueItemRow.recipient_email == email,
                MailQueueItemRow.mail_type == "account_password_otp",
            )
            .order_by(MailQueueItemRow.created_at.desc())
        )
        return re.search(r"인증번호: ([0-9]{6})", row.body_text).group(1)


def register(c):
    assert (
        client.get("/api/auth/general/password", headers=c["owner"]).json()["data"][
            "has_password"
        ]
        is False
    )
    assert (
        client.post(
            "/api/auth/general/password/request", headers=c["owner"]
        ).status_code
        == 200
    )
    code = code_for(email_for(c))
    result = client.post(
        "/api/auth/general/password/confirm",
        headers=c["owner"],
        json={"otp_code": code, "new_password": PASSWORD},
    )
    assert result.status_code == 200, result.text
    return code


def test_set_password_revoke_all_sessions_and_password_login_preserves_conflicts(
    context,
):
    c = context
    email = email_for(c)
    code = register(c)
    assert client.get("/api/auth/general/me", headers=c["owner"]).status_code == 401
    with SessionLocal() as db:
        row = db.get(AccountPasswordRow, email)
        assert PASSWORD not in row.password_hash and verify_password(
            PASSWORD, row.password_hash
        )
        assert db.get(PasswordChallengeRow, (email, "set")) is None
    values = {"email": email, "password": PASSWORD}
    assert client.post("/api/auth/general/password/login", json=values).status_code == 403
    authenticate_environment(email)
    first = client.post("/api/auth/general/password/login", json=values)
    assert first.status_code == 200, first.text
    token = first.json()["data"]["access_token"]
    assert (
        client.post("/api/auth/general/password/login", json=values).status_code == 409
    )
    replacement = client.post(
        "/api/auth/general/password/login", json={**values, "force_new_session": True}
    )
    assert replacement.status_code == 200
    assert (
        client.get(
            "/api/auth/general/me", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/general/password/login", json={**values, "password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/general/password/confirm",
            headers=c["owner"],
            json={"otp_code": code, "new_password": PASSWORD},
        ).status_code
        == 401
    )


def test_reset_uses_independent_single_use_proof_and_hides_registration(context):
    c = context
    email = email_for(c)
    register(c)
    with SessionLocal() as db:
        db.execute(
            delete(AuthRateLimitRow).where(
                AuthRateLimitRow.key.startswith("pwd-mail-cooldown:")
            )
        )
        db.commit()
    known = client.post(
        "/api/auth/general/password/reset/request", json={"email": email}
    )
    unknown = client.post(
        "/api/auth/general/password/reset/request",
        json={"email": uuid4().hex + "@example.com"},
    )
    assert known.json()["data"] == unknown.json()["data"]
    code = code_for(email)
    values = {"email": email, "otp_code": code, "new_password": PASSWORD + "Changed"}
    assert (
        client.post(
            "/api/auth/general/password/reset/confirm",
            json={**values, "otp_code": "000000"},
        ).status_code
        == 401
    )
    assert (
        client.post("/api/auth/general/password/reset/confirm", json=values).status_code
        == 200
    )
    assert (
        client.post("/api/auth/general/password/reset/confirm", json=values).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/general/password/login",
            json={"email": email, "password": PASSWORD},
        ).status_code
        == 401
    )
    assert client.post("/api/auth/general/password/login", json={"email": email, "password": PASSWORD + "Changed"}).status_code == 403
    authenticate_environment(email)
    assert client.post("/api/auth/general/password/login", json={"email": email, "password": PASSWORD + "Changed"}).status_code == 200


def test_attempts_expiry_wrong_purpose_and_no_password_echo(context):
    c = context
    email = email_for(c)
    client.post("/api/auth/general/password/request", headers=c["owner"])
    code = code_for(email)
    assert (
        client.post(
            "/api/auth/general/password/reset/confirm",
            json={"email": email, "otp_code": code, "new_password": PASSWORD},
        ).status_code
        == 401
    )
    for _ in range(5):
        assert (
            client.post(
                "/api/auth/general/password/confirm",
                headers=c["owner"],
                json={"otp_code": "000000", "new_password": PASSWORD},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/auth/general/password/confirm",
            headers=c["owner"],
            json={"otp_code": code, "new_password": PASSWORD},
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        row = db.get(PasswordChallengeRow, (email, "set"))
        row.attempts = 0
        row.expires_at = int(time.time()) - 1
        db.commit()
    assert (
        client.post(
            "/api/auth/general/password/confirm",
            headers=c["owner"],
            json={"otp_code": code, "new_password": PASSWORD},
        ).status_code
        == 401
    )
    secret = "private-short"
    result = client.post(
        "/api/auth/general/password/confirm",
        headers=c["owner"],
        json={"otp_code": code, "new_password": secret},
    )
    assert result.status_code == 422 and secret not in result.text
    assert (
        client.post(
            "/api/auth/general/password/confirm",
            headers=c["owner"],
            json={
                "otp_code": code,
                "new_password": PASSWORD,
                "email": "foreign@example.com",
            },
        ).status_code
        == 422
    )


def test_email_login_remains_available_and_otp_is_single_use(context):
    c = context
    email = email_for(c)
    otp = store.create_general_otp(email)
    values = {"email": email, "otp_code": otp, "force_new_session": True}
    assert client.post("/api/auth/general/otp/verify", json=values).status_code == 200
    assert client.post("/api/auth/general/otp/verify", json=values).status_code == 401


def authenticate_environment(email):
    code = store.create_general_otp(email)
    result = client.post("/api/auth/general/otp/verify", json={"email":email, "otp_code":code, "force_new_session":True})
    assert result.status_code == 200, result.text
    session = result.json()["data"]
    store.revoke_general_session(session["access_token"], session["refresh_token"])
