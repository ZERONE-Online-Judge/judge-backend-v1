"""Direct fixture setup for contest schedules that are immutable in production."""

from enum import Enum

from app.orm_models import ContestRow
from app.services.store import store


def set_contest_schedule(contest_id: str, **values):
    """Set test fixtures directly; production schedule protections still apply.

    Lifecycle tests reuse ended contests to exercise earlier contest phases.
    Only their schedule setup bypasses the public settings operation. Other
    settings still pass through the normal store policy and normalization.
    """
    schedule = {
        field: values.pop(field)
        for field in ("status", "start_at", "freeze_at", "end_at")
        if field in values
    }
    with store._session() as db:
        row = db.get(ContestRow, contest_id)
        assert row is not None, f"Missing contest fixture: {contest_id}"
        for field, value in schedule.items():
            setattr(row, field, value.value if isinstance(value, Enum) else value)
        db.commit()
    if values:
        return store.update_contest_settings(contest_id, **values)
    return store.get_contest(contest_id)
