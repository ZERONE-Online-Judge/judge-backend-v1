"""Remembered browser/IP lists survive login and session changes independently."""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.orm_models import AccountPasswordRow, TrustedEnvironmentRow
from app.services import login_security
from app.services.account_passwords import hash_password
from app.services.security import token_hash
from app.services.store import store
from test_passkeys import PASSWORD, PREFIX, assertion, browser, enroll, login


def password_values(email, **overrides):
    return {"email": email, "password": PASSWORD, "force_new_session": True, **overrides}


def add_password(email):
    with SessionLocal() as db:
        db.add(AccountPasswordRow(email=email, password_hash=hash_password(PASSWORD), updated_at=int(time.time())))
        db.commit()


def verify_email(client, email):
    code = store.create_general_otp(email)
    response = client.post(PREFIX + "/otp/verify", json={"email": email, "otp_code": code, "force_new_session": True})
    assert response.status_code == 200, response.text
    return response


def environments(email):
    with SessionLocal() as db:
        return {(row.device_hash, row.ip_hash): row.expires_at for row in db.scalars(
            select(TrustedEnvironmentRow).where(TrustedEnvironmentRow.email == email)
        )}


@pytest.mark.parametrize("verification", ["email", "passkey"])
def test_same_browser_retains_both_ips_across_transfers_and_logout(browser, verification):
    email, headers, session = login(browser)
    add_password(email)
    authenticator = enroll(browser, headers) if verification == "passkey" else None
    device = browser.cookies.get(login_security.cookie_name())
    old_token = session["access_token"]
    with SessionLocal() as db:
        row = db.get(TrustedEnvironmentRow, (email, token_hash(device), login_security.ip_key("203.0.113.7")))
        row.expires_at = int(time.time()) + 60
        db.commit()
    with TestClient(app, base_url="https://zoj.kr", client=("203.0.113.8", 50001)) as second_ip:
        second_ip.cookies.update(browser.cookies)
        assert second_ip.post(PREFIX + "/password/login", json=password_values(email)).status_code == 403
        if verification == "email":
            verify_email(second_ip, email)
        else:
            response = second_ip.post(PREFIX + "/passkeys/login/verify", json={**assertion(second_ip, authenticator), "force_new_session": True})
            assert response.status_code == 200, response.text
        assert second_ip.cookies.get(login_security.cookie_name()) == device
        saved = environments(email)
        assert set(saved) == {(token_hash(device), login_security.ip_key(ip)) for ip in ["203.0.113.7", "203.0.113.8"]}
        assert min(saved.values()) >= int(time.time()) + login_security.TRUST_SECONDS - 2
        assert browser.get(PREFIX + "/me", headers={"Authorization": "Bearer " + old_token}).status_code == 401
        # A revoked session can log out without erasing this browser's IP list.
        browser.post(PREFIX + "/logout", json={})
        assert browser.cookies.get(login_security.cookie_name()) == device
        for client in [browser, second_ip, browser]:
            response = client.post(PREFIX + "/password/login", json=password_values(email))
            assert response.status_code == 200, response.text
            assert response.cookies.get(login_security.cookie_name()) == device
            # Remembered environments do not authorize credential management.
            token = response.json()["data"]["access_token"]
            assert client.post(PREFIX + "/passkeys/register/options", headers={"Authorization": "Bearer " + token}).status_code == 403
        assert set(environments(email)) == set(saved)


def test_password_login_renews_only_this_accounts_browser_list(browser):
    email, _, _ = login(browser)
    add_password(email)
    device = browser.cookies.get(login_security.cookie_name())
    now = int(time.time())
    other_email = "other-" + email
    other_device = token_hash("different-browser")
    current = (token_hash(device), login_security.ip_key("203.0.113.7"))
    remembered = (token_hash(device), login_security.ip_key("203.0.113.8"))
    expired = (token_hash(device), login_security.ip_key("203.0.113.9"))
    with SessionLocal() as db:
        db.get(TrustedEnvironmentRow, (email, *current)).expires_at = now + 60
        db.add_all([
            TrustedEnvironmentRow(email=email, device_hash=remembered[0], ip_hash=remembered[1], expires_at=now + 60),
            TrustedEnvironmentRow(email=email, device_hash=expired[0], ip_hash=expired[1], expires_at=now - 1),
            TrustedEnvironmentRow(email=email, device_hash=other_device, ip_hash=current[1], expires_at=now + 120),
            TrustedEnvironmentRow(email=other_email, device_hash=current[0], ip_hash=current[1], expires_at=now + 120),
        ])
        db.commit()
    response = browser.post(PREFIX + "/password/login", json=password_values(email))
    assert response.status_code == 200, response.text
    values = environments(email)
    assert values[current] == values[remembered]
    assert values[current] >= now + login_security.TRUST_SECONDS
    assert values[expired] == now - 1
    assert values[(other_device, current[1])] == now + 120
    assert environments(other_email)[current] == now + 120
    assert f"Max-Age={login_security.TRUST_SECONDS}" in response.headers["set-cookie"]


def test_other_browsers_keep_their_own_trust_after_session_transfer(browser):
    email, _, _ = login(browser)
    add_password(email)
    device = browser.cookies.get(login_security.cookie_name())
    with TestClient(app, base_url="https://zoj.kr", client=("203.0.113.7", 50002)) as other:
        # Matching the IP alone is insufficient without this browser's cookie.
        assert other.post(PREFIX + "/password/login", json=password_values(email)).status_code == 403
        verify_email(other, email)
        other_device = other.cookies.get(login_security.cookie_name())
        assert other_device != device
        for client in [browser, other, browser]:
            assert client.post(PREFIX + "/password/login", json=password_values(email)).status_code == 200
        assert {key[0] for key in environments(email)} == {token_hash(device), token_hash(other_device)}


@pytest.mark.parametrize("failure", ["wrong_password", "session_conflict", "expired", "new_ip"])
def test_failed_login_never_creates_or_renews_trust(browser, failure):
    email, _, _ = login(browser)
    add_password(email)
    now = int(time.time())
    with SessionLocal() as db:
        row = db.scalar(select(TrustedEnvironmentRow).where(TrustedEnvironmentRow.email == email))
        row.expires_at = now - 1 if failure == "expired" else now + 60
        db.commit()
    before = environments(email)
    values = password_values(email)
    if failure == "wrong_password":
        values["password"] = "incorrect"
    elif failure == "session_conflict":
        values["force_new_session"] = False
    if failure == "new_ip":
        with TestClient(app, base_url="https://zoj.kr", client=("203.0.113.99", 50003)) as other:
            other.cookies.update(browser.cookies)
            response = other.post(PREFIX + "/password/login", json=values)
    else:
        response = browser.post(PREFIX + "/password/login", json=values)
    assert response.status_code == {"wrong_password": 401, "session_conflict": 409, "expired": 403, "new_ip": 403}[failure]
    assert "set-cookie" not in response.headers
    assert environments(email) == before


def test_clearing_device_cookie_still_requires_verification(browser):
    email, _, _ = login(browser)
    add_password(email)
    before = environments(email)
    browser.cookies.clear()
    response = browser.post(PREFIX + "/password/login", json=password_values(email))
    assert response.status_code == 403
    assert environments(email) == before


@pytest.mark.parametrize("change", ["password", "passkey"])
def test_security_changes_clear_the_complete_browser_ip_list(browser, change):
    email, headers, _ = login(browser)
    add_password(email)
    auth = enroll(browser, headers)
    with TestClient(app, base_url="https://zoj.kr", client=("203.0.113.8", 50004)) as second_ip:
        second_ip.cookies.update(browser.cookies)
        verified = verify_email(second_ip, email).json()["data"]
        headers = {"Authorization": "Bearer " + verified["access_token"]}
        assert len(environments(email)) == 2
        if change == "passkey":
            from webauthn.helpers import bytes_to_base64url
            response = second_ip.post(PREFIX + "/passkeys/remove", headers=headers, json={"credential_id": bytes_to_base64url(auth.id)})
            assert response.status_code == 200, response.text
        else:
            from app.services.password_auth import request_change, confirm_change
            from test_account_passwords import code_for
            request_change(email, "reset", "203.0.113.8")
            confirm_change(email, "reset", code_for(email), PASSWORD + "Reset", "203.0.113.8")
        assert not environments(email)
        password = PASSWORD + "Reset" if change == "password" else PASSWORD
        for client in [browser, second_ip]:
            assert client.post(PREFIX + "/password/login", json=password_values(email, password=password)).status_code == 403
