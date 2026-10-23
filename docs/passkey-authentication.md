# Passkeys and password login verification

ZOJ supports discoverable WebAuthn passkeys. The browser handles device
verification (biometrics or PIN); ZOJ stores a credential ID, public key,
account UUID, signature counter, backup state and user-facing name. Private
keys and biometric data never enter the application.

## User flows

- After successful email OTP login, accounts without passkeys are offered
  enrollment. Skipping or cancelling leaves the authenticated session usable.
- The login page first asks only for an email. The next step prefers a
  registered passkey on supported browsers, otherwise a registered password,
  otherwise an email code. Other available methods stay on the same card.
- Password login from an unfamiliar browser/IP automatically requests an email
  code. A registered passkey can complete verification instead. Switching to
  another email resets the selected methods and verification state.
- My account → Passkey management lists credentials and their last use,
  supports adding up to ten credentials, and confirms deletion.
- Adding/deleting credentials requires a strong verification within five
  minutes. An existing password-only or old session can verify a separate,
  session-bound email code in the management dialog.
- Email OTP remains the recovery path when a device or passkey is unavailable.

## Password login policy

A correct password alone can issue a session only when a previous email or
passkey login verified both this browser's HttpOnly cookie and its current IP.
Trust lasts 30 days from the last strong login, without renewal by password
login or token refresh. Missing cookies, changed IPs, expired trust and all
predeployment environments require email OTP or a passkey before login.
There is no session, SSO cookie or operator access issued by a password
attempt that requires this additional verification.

The production device cookie uses `__Host-`, Secure, HttpOnly, SameSite=Lax,
Path=/ and no Domain. The DB contains only its SHA-256 digest and an HMAC of
the normalized IP. Trust is scoped per email, browser token and IP. Password
registration/reset and passkey deletion revoke remembered environments.
Password changes also revoke existing sessions and pending security challenges.

Only ASGI `request.client.host` is used for trust and limits; authentication
never parses untrusted forwarding headers itself. Production Uvicorn must
retain a restricted `FORWARDED_ALLOW_IPS` matching the actual reverse proxies;
never configure `*`. Editor's signed password bridge is also gated and cannot
bypass verification; Editor can use email sign-in or restore the ZOJ SSO session.
IP is a risk signal, not a second authentication factor by itself.

## Ceremony validation

`PUBLIC_BASE_URL` defines the exact allowed origin and RP hostname. Production
must retain `https://zoj.kr`; changing the RP hostname makes existing passkeys
unusable on the new site. Arbitrary request Host/Origin headers cannot select
another RP. User verification and user presence are required, registration is
bound to the active session/account, and assertions verify the account's user
handle, challenge, RP ID, origin, signature, flags and signature counter.
Synced passkeys with zero counters are supported by the standard verifier.

New login clients send the selected email when requesting passkey options.
The server restricts allowed credentials to that account and stores the email
in the challenge. Verification checks the credential owner's email against
that stored value, so changing the browser's allowed list cannot switch accounts.
Unknown emails and accounts without credentials return `passkey_unavailable`.
Method lookup returns only password/passkey availability, with per-email and
shared-IP limits. Legacy requests without a body remain supported during the
rolling frontend upgrade; they retain the original discoverable login behavior.

Challenges expire after five minutes, are DB-backed across workers, and are
atomically consumed even on a failed signature. Management email codes are
HMAC digests, expire after five minutes, allow at most five failed attempts,
and are bound to one session and purpose. Rate limits apply to ceremony and
email endpoints. No raw passwords, codes, signed responses or public key
material are added to access logs. Credential add/remove operations send
security notification emails and create account-attributed access events.

## Validation and release

`tests/test_passkeys.py` uses real ES256 keys, CBOR attestation objects and
signed assertions against the unmocked WebAuthn verifier. It covers tampering,
wrong RP/origin/UV, replay, counter rollback, synced credentials, duplicate and
limit handling, ownership, stale/revoked sessions, IP/browser verification,
password reset and production cookie flags. Frontend interaction tests cover
OTP enrollment/skip, passkey cancellation, additional verification and
management email verification/deletion confirmation. Migration tests cover
upgrade and downgrade. Physical authenticator dialogs require the user's own
interaction; automated tests do not register credentials for production users.

Deploy backend migration 0054 before the frontend. The additive tables allow
rolling back application code without dropping credentials. Do not downgrade
0054 on production merely to roll back the UI, because that deletes passkeys.

References: [WebAuthn](https://www.w3.org/TR/webauthn-3/),
[py_webauthn](https://duo-labs.github.io/py_webauthn/),
[OWASP MFA](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html).
