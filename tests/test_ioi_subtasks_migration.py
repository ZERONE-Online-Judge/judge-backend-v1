"""IOI rollout is additive for SQLite and real disposable PostgreSQL."""
import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.test_presentation_alert_migration import connection


@pytest.fixture
def migration():
    path = Path(__file__).parents[1] / "migrations/versions/0066_ioi_subtasks.py"
    spec = importlib.util.spec_from_file_location("ioi_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0065_presentation_alert_minutes"
    return module


def seed(connection):
    connection.execute(sa.text("CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY, title TEXT NOT NULL)"))
    connection.execute(sa.text("CREATE TABLE testcase_sets (testcase_set_id VARCHAR(36) PRIMARY KEY, problem_id VARCHAR(36) NOT NULL)"))
    connection.execute(sa.text("CREATE TABLE submissions (submission_id VARCHAR(36) PRIMARY KEY, contest_id VARCHAR(36) NOT NULL REFERENCES contests(contest_id), status VARCHAR(32) NOT NULL)"))
    connection.execute(sa.text("INSERT INTO contests VALUES ('contest', 'Saved contest')"))
    connection.execute(sa.text("INSERT INTO testcase_sets VALUES ('set', 'problem')"))
    connection.execute(sa.text("INSERT INTO submissions VALUES ('submission', 'contest', 'accepted')"))


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def test_existing_icpc_data_backfills_without_reinterpreting_verdicts(connection, migration, monkeypatch):
    seed(connection)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    migration.upgrade()
    migration.upgrade()
    row = connection.execute(sa.text("SELECT * FROM submissions")).mappings().one()
    assert row["status"] == "accepted" and row["scoring_mode"] == "icpc"
    assert row["score"] is None and row["max_score"] is None
    assert decode(row["subtask_results"]) == [] and decode(row["scoring_evidence"]) == []
    assert decode(row["scoring_snapshot"]) == {}
    assert connection.execute(sa.text("SELECT scoring_mode FROM contests")).scalar_one() == "icpc"
    assert decode(connection.execute(sa.text("SELECT subtasks FROM testcase_sets")).scalar_one()) == []
    connection.execute(sa.text("INSERT INTO contests (contest_id, title) VALUES ('new', 'New')"))
    assert connection.execute(sa.text("SELECT scoring_mode FROM contests WHERE contest_id='new'")).scalar_one() == "icpc"
    for table, names in {"contests": ["scoring_mode"], "testcase_sets": ["subtasks"],
        "submissions": ["scoring_mode", "subtask_results", "scoring_snapshot", "scoring_evidence"]}.items():
        columns = {item["name"]: item for item in sa.inspect(connection).get_columns(table)}
        assert all(not columns[name]["nullable"] for name in names)


def test_partial_rollout_preserves_existing_scores_and_downgrade_keeps_old_rows(connection, migration, monkeypatch):
    seed(connection)
    connection.execute(sa.text("ALTER TABLE contests ADD COLUMN scoring_mode VARCHAR(16) NOT NULL DEFAULT 'ioi'"))
    connection.execute(sa.text("ALTER TABLE submissions ADD COLUMN score INTEGER"))
    connection.execute(sa.text("UPDATE submissions SET score=70"))
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    migration.upgrade()
    assert connection.execute(sa.text("SELECT score FROM submissions")).scalar_one() == 70
    assert connection.execute(sa.text("SELECT scoring_mode FROM contests")).scalar_one() == "ioi"
    migration.downgrade()
    assert connection.execute(sa.text("SELECT status FROM submissions")).scalar_one() == "accepted"
    assert connection.execute(sa.text("SELECT title FROM contests")).scalar_one() == "Saved contest"
    assert connection.execute(sa.text("SELECT problem_id FROM testcase_sets")).scalar_one() == "problem"
