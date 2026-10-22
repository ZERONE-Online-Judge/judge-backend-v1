"""Legacy repair must recover exact history without guessing file identity."""
import hashlib
import importlib.util
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models import now_utc
from app.orm_models import ProblemAssetRow, SubmissionRow, VerificationRunRow, VerificationTrialRow
from app.services import verification_ai as ai
from app.services.store import store
from test_verification_ai import context, submit


def migrate(c, monkeypatch):
    path = Path(__file__).parents[1] / "migrations/versions/0039_restore_verification_runs.py"
    spec = importlib.util.spec_from_file_location("restore_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with c["sessions"].kw["bind"].begin() as db:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db)))
        migration.upgrade()


def legacy(c, source=None, **values):
    submission = store.create_operator_test_submission(
        c["cid"], c["pid"], "python311", source or c["source"],
        submitted_by_name="old operator",
    )
    with c["sessions"]() as db:
        row = db.get(SubmissionRow, submission.submission_id)
        row.language = "python313"  # A stored pre-policy submission.
        row.status = "wrong_answer"
        row.judge_message = "original stored diagnosis"
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()
    return submission.submission_id


def test_repair_preserves_verdict_order_existing_analysis_and_is_idempotent(context, monkeypatch):
    c = context
    first = legacy(c)
    tracked = submit(c)
    ai.request_analysis(c["cid"], c["pid"], tracked)
    ai.process_one()
    analysis_id = ai.analysis_detail(c["cid"], c["pid"], tracked)["analysis"]["analysis_id"]
    last = legacy(c)
    migrate(c, monkeypatch)
    migrate(c, monkeypatch)
    with c["sessions"]() as db:
        rows = db.scalars(select(VerificationRunRow)).all()
        assert {row.submission_id for row in rows} == {first, tracked, last}
        assert db.get(VerificationRunRow, tracked).analysis_id == analysis_id
        for sid in (first, last):
            row = db.get(VerificationRunRow, sid)
            assert row.created_at == db.get(SubmissionRow, sid).submitted_at
            assert row.asset_id == c["aid"]
            assert row.context_hash is None and row.analysis_id is None
    latest = ai.list_runs(c["cid"], c["pid"])["runs"]
    assert len(latest) == 1
    assert latest[0]["submission"]["submission_id"] == last
    assert latest[0]["submission"]["judge_message"] == "original stored diagnosis"
    assert latest[0]["submission"]["submitted_by_name"] == "old operator"
    assert latest[0]["snapshot_available"] is False
    assert latest[0]["analysis"] is None
    assert len(c["calls"]) == 1


def test_repair_skips_ambiguous_predating_participant_and_ai_trial_records(context, monkeypatch):
    c = context
    before = legacy(c, submitted_at=now_utc() - timedelta(days=1))
    participant = legacy(c, submission_kind="participant")
    trial = legacy(c)
    mismatch = legacy(c, source="print('different source')\n")
    duplicate_source = "print('two files')\n"
    with c["sessions"]() as db:
        db.add(VerificationTrialRow(
            submission_id=trial, analysis_id=str(uuid4()), contest_id=c["cid"],
            problem_id=c["pid"], context_hash="context", cache_key=str(uuid4()),
            artifact_id="original", testcase_count=1,
        ))
        for kind in ("accepted", "wrong_answer"):
            db.add(ProblemAssetRow(
                contest_id=c["cid"], problem_id=c["pid"], original_filename="duplicate.py",
                storage_key=f'problems/{c["pid"]}/verification-solutions/{kind}/duplicate.py',
                sha256=hashlib.sha256(duplicate_source.encode()).hexdigest(),
                file_size=len(duplicate_source), mime_type="text/plain", asset_status="active",
            ))
        db.commit()
    duplicate = legacy(c, source=duplicate_source)
    valid = legacy(c)
    migrate(c, monkeypatch)
    with c["sessions"]() as db:
        ids = set(db.scalars(select(VerificationRunRow.submission_id)))
    assert ids == {valid}
    assert ids.isdisjoint({before, participant, trial, mismatch, duplicate})
    assert not c["calls"]


def test_repair_streams_multiple_batches_without_losing_or_duplicating_records(context, monkeypatch):
    c = context
    base = store.create_operator_test_submission(c["cid"], c["pid"], "python311", c["source"])
    with c["sessions"]() as db:
        division_id = db.get(SubmissionRow, base.submission_id).division_id
        db.add_all([
            SubmissionRow(
                contest_id=c["cid"], problem_id=c["pid"], division_id=division_id,
                submission_kind="operator_test", language="python313", source_code=c["source"],
                status="accepted",
            ) for _ in range(251)
        ])
        db.commit()
    migrate(c, monkeypatch)
    migrate(c, monkeypatch)
    with c["sessions"]() as db:
        runs = db.scalars(select(VerificationRunRow)).all()
    assert len(runs) == len({r.submission_id for r in runs}) == 252
    assert not c["calls"]
