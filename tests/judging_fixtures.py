"""Give successful judging fixtures real input without changing demo seeding."""
import hashlib

from sqlalchemy import func, select

from app.orm_models import TestcaseRow, TestcaseSetRow
from app.services.storage import object_storage
from app.services.store import store


def ensure_judging_inputs(problem_id):
    problem = store.get_problem(problem_id)
    with store._session() as db:
        active = db.scalar(select(TestcaseSetRow).where(
            TestcaseSetRow.problem_id == problem_id, TestcaseSetRow.is_active.is_(True)))
        if active and db.scalar(select(TestcaseRow.testcase_id).where(
            TestcaseRow.testcase_set_id == active.testcase_set_id).limit(1)):
            return
        if active is None:
            version = (db.scalar(select(func.max(TestcaseSetRow.version)).where(
                TestcaseSetRow.problem_id == problem_id)) or 0) + 1
            active = TestcaseSetRow(problem_id=problem_id, version=version, is_active=True)
            db.add(active)
            db.flush()
        key = f'contests/{problem.contest_id}/problems/{problem_id}/fixture-empty'
        object_storage.write_bytes(key, b'')
        sha = hashlib.sha256(b'').hexdigest()
        db.add(TestcaseRow(testcase_set_id=active.testcase_set_id, display_order=1,
            input_storage_key=key, output_storage_key=key,
            input_sha256=sha, output_sha256=sha))
        db.commit()
