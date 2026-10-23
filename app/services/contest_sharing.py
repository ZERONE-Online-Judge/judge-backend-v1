"""Stable short URLs identify a contest without granting access to it."""

import re
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.orm_models import ContestRow, ContestShareLinkRow
from app.services.errors import not_found


def get_or_create(contest_id: str) -> str:
    with SessionLocal() as db:
        # Serialize creation for the same contest, including separate workers.
        contest = db.scalar(
            select(ContestRow)
            .where(ContestRow.contest_id == contest_id)
            .with_for_update()
        )
        if not contest:
            raise not_found()
        existing = db.scalar(
            select(ContestShareLinkRow).where(
                ContestShareLinkRow.contest_id == contest_id
            )
        )
        if existing:
            return existing.code
        for _ in range(5):
            code = secrets.token_urlsafe(8)
            try:
                with db.begin_nested():
                    db.add(ContestShareLinkRow(code=code, contest_id=contest_id))
                    db.flush()
                db.commit()
                return code
            except IntegrityError:
                # Also handle concurrent SQLite creation and rare code collisions.
                existing = db.scalar(
                    select(ContestShareLinkRow).where(
                        ContestShareLinkRow.contest_id == contest_id
                    )
                )
                if existing:
                    return existing.code
        raise RuntimeError("Unable to allocate a unique contest share code")


def resolve(code: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", code):
        raise not_found()
    with SessionLocal() as db:
        contest_id = db.scalar(
            select(ContestRow.contest_id)
            .join(
                ContestShareLinkRow,
                ContestShareLinkRow.contest_id == ContestRow.contest_id,
            )
            .where(ContestShareLinkRow.code == code)
        )
        if not contest_id:
            raise not_found()
        # Normal participant routes still enforce login, membership and visibility.
        return contest_id
