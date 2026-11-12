from datetime import timedelta, timezone
import secrets

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import SessionLocal
from app.models import new_id, now_utc
from app.orm_models import ContestRow, PresentationAccountRow, PresentationLoginLimitRow
from app.services.errors import AppError, authentication_required, not_found
from app.services.security import decode_session_token, new_session_token, token_hash

DOMAIN = "score.zoj.kr"
# Eight easy-to-type characters, with no 0/o/1/i/l confusion.
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
LIFETIME = timedelta(days=7)
LOGIN_ATTEMPTS_PER_MINUTE = 20


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_presentation_email(email: str) -> bool:
    return email.strip().lower().endswith("@" + DOMAIN)


def describe(row: PresentationAccountRow) -> dict:
    return {"email": row.email, "created_at": aware(row.created_at).isoformat(),
            "expires_at": aware(row.expires_at).isoformat(), "active": aware(row.expires_at) > now_utc()}


def get_account(contest_id: str) -> dict | None:
    with SessionLocal() as db:
        if not db.get(ContestRow, contest_id):
            raise not_found()
        row = db.get(PresentationAccountRow, contest_id)
        return describe(row) if row else None


def issue_account(contest_id: str) -> dict:
    with SessionLocal.begin() as db:
        if not db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update()):
            raise not_found()
        row = db.get(PresentationAccountRow, contest_id)
        if row is None:
            row = PresentationAccountRow(contest_id=contest_id)
            db.add(row)
        while True:
            email = "".join(secrets.choice(ALPHABET) for _ in range(8)) + "@" + DOMAIN
            if not db.scalar(select(PresentationAccountRow.contest_id).where(PresentationAccountRow.email == email)):
                break
        row.email, row.credential_id = email, new_id()
        row.created_at = now_utc()
        row.expires_at = row.created_at + LIFETIME
        row.session_token_hash = None
        db.flush()
        return describe(row)


def revoke_account(contest_id: str) -> None:
    with SessionLocal.begin() as db:
        if not db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update()):
            raise not_found()
        db.execute(delete(PresentationAccountRow).where(PresentationAccountRow.contest_id == contest_id))


def throttle_login(client_key: str) -> None:
    """Atomic DB counter shared by all application workers; count every attempt."""
    now = now_utc()
    with SessionLocal.begin() as db:
        table = PresentationLoginLimitRow
        insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
        reset = table.window_start <= now - timedelta(minutes=1)
        statement = insert(table).values(client_key=token_hash(client_key), window_start=now, attempts=1)
        statement = statement.on_conflict_do_update(index_elements=[table.client_key], set_={
            "window_start": case((reset, now), else_=table.window_start),
            "attempts": case((reset, 1), else_=table.attempts + 1),
        }).returning(table.attempts)
        attempts = db.scalar(statement)
        db.execute(delete(table).where(table.window_start < now - timedelta(days=1)))
    if attempts > LOGIN_ATTEMPTS_PER_MINUTE:
        raise AppError(429, "presentation_login_rate_limited", "로그인 시도가 많습니다. 1분 후 다시 시도해 주세요.", {"retry_after_seconds": 60})


def login(email: str, client_key: str) -> dict:
    throttle_login(client_key)
    normalized = email.strip().lower()
    with SessionLocal.begin() as db:
        row = db.scalar(select(PresentationAccountRow).where(PresentationAccountRow.email == normalized).with_for_update())
        if row is None or aware(row.expires_at) <= now_utc():
            raise AppError(401, "invalid_presentation_account", "프레젠테이션 계정을 확인해 주세요. 만료되거나 사용 중지된 계정은 로그인할 수 없습니다.")
        ttl = int((aware(row.expires_at) - now_utc()).total_seconds())
        if ttl <= 0:
            raise authentication_required()
        token = new_session_token("presentation", row.contest_id, ttl, {"credential_id": row.credential_id})
        return {"access_token": token, "contest_id": row.contest_id, "expires_at": aware(row.expires_at).isoformat()}


def require_presentation(token: str | None, contest_id: str) -> None:
    claims = decode_session_token(token, "presentation") if token else None
    if not claims or claims.get("sub") != contest_id:
        raise authentication_required("프레젠테이션 전용 로그인이 필요합니다.")
    with SessionLocal() as db:
        row = db.get(PresentationAccountRow, contest_id)
        # A display credential may be used by several screens at once. Its
        # generation and expiry revoke every signed session when the account
        # is reissued, disabled or expired, without displacing other screens.
        if (row is None or aware(row.expires_at) <= now_utc()
                or row.credential_id != claims.get("credential_id")):
            raise authentication_required("프레젠테이션 계정이 만료되었거나 사용 중지되었습니다.")
