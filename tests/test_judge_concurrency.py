"""Run against a disposable PostgreSQL with ZOJ_TEST_POSTGRES_URL set.

Each test uses its own schema; no existing tables are read or modified.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import Base
from app.models import SubmissionStatus, now_utc
from app.orm_models import ContestRow, ContestDivisionRow, ProblemRow, SubmissionRow, JudgeJobRow
from app.services.store import store
from app.tools import judge_nodes


SECRET = "concurrency-test-approved-node-secret"
pytestmark = pytest.mark.skipif(not os.getenv("ZOJ_TEST_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


@pytest.fixture
def pg_database(monkeypatch):
    url = os.environ["ZOJ_TEST_POSTGRES_URL"]
    schema = "judge_security_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=10000"})
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(store, "_session", sessions)
        monkeypatch.setattr(judge_nodes, "SessionLocal", sessions)
        node = store.provision_node("approved-node", SECRET, 2)
        with sessions() as db:
            db.add(ContestRow(contest_id="contest", title="Test", organization_name="Test", overview="",
                              status="running", start_at=now_utc(), end_at=now_utc(), freeze_at=now_utc()))
            db.flush()
            db.add(ContestDivisionRow(division_id="division", contest_id="contest", code="A", name="Test"))
            db.flush()
            db.add(ProblemRow(problem_id="problem", contest_id="contest", division_id="division", problem_code="A",
                              title="Test", statement="", time_limit_ms=1000, memory_limit_mb=128, display_order=1))
            db.flush()
            db.add(SubmissionRow(submission_id="submission", contest_id="contest", division_id="division",
                                 problem_id="problem", language="python311", source_code="print(42)", status="judging"))
            db.flush()
            db.add(JudgeJobRow(judge_job_id="job", submission_id="submission", contest_id="contest", division_id="division",
                               status="running", queue_position=1, assigned_node_id=node.judge_node_id,
                               lease_token="lease", leased_at=now_utc()))
            db.commit()
        yield sessions, node
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def race(*operations):
    barrier = Barrier(len(operations))

    def run(operation):
        barrier.wait(timeout=10)
        try:
            operation()
            return "ok"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def finalize(status):
    return store.report_judge_result("job", SECRET, "lease", status, None, None, None)


def test_concurrent_final_results_have_exactly_one_winner(pg_database):
    sessions, _ = pg_database
    outcomes = race(lambda: finalize(SubmissionStatus.ACCEPTED), lambda: finalize(SubmissionStatus.WRONG_ANSWER))
    assert sorted(outcomes) == ["ok", "rejected"]
    with sessions() as db:
        assert db.get(JudgeJobRow, "job").lease_token is None
        assert db.get(SubmissionRow, "submission").status in {"accepted", "wrong_answer"}


def test_concurrent_progress_cannot_reopen_a_final_result(pg_database):
    sessions, _ = pg_database
    outcomes = race(lambda: finalize(SubmissionStatus.ACCEPTED), lambda: store.update_judge_progress(
        "job", SECRET, "lease", SubmissionStatus.JUDGING, 1, 2))
    assert outcomes[0] == "ok"
    with sessions() as db:
        assert db.get(JudgeJobRow, "job").status == "succeeded"
        assert db.get(SubmissionRow, "submission").status == "accepted"


def test_concurrent_revocation_and_claim_leave_no_work_owned_by_revoked_node(pg_database):
    sessions, node = pg_database
    with sessions() as db:
        job = db.get(JudgeJobRow, "job")
        job.status, job.assigned_node_id, job.lease_token, job.leased_at = "pending", None, None, None
        db.commit()
    outcomes = race(lambda: judge_nodes.update_credential(node.node_name, enabled=False),
                    lambda: store.claim_jobs(node.judge_node_id, SECRET, 1))
    assert outcomes[0] == "ok"
    with sessions() as db:
        job = db.get(JudgeJobRow, "job")
        assert job.status == "pending"
        assert job.assigned_node_id is None
        assert job.lease_token is None
