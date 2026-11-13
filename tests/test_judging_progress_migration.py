import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.test_presentation_alert_migration import connection


def test_retry_phase_migration_is_additive_idempotent_and_reversible(connection, monkeypatch):
    path = Path(__file__).parents[1] / "migrations/versions/0068_judging_progress_phase.py"
    spec = importlib.util.spec_from_file_location("progress_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0067_freeze_count_default"
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    for table in ("submissions", "editor_judge_runs"):
        connection.execute(sa.text(f"CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY, status VARCHAR(32) NOT NULL)"))
        connection.execute(sa.text(f"INSERT INTO {table} VALUES ('saved', 'accepted')"))
    migration.upgrade()
    migration.upgrade()
    for table in ("submissions", "editor_judge_runs"):
        row = connection.execute(sa.text(f"SELECT * FROM {table}")).mappings().one()
        assert row["status"] == "accepted" and row["progress_phase"] is None
    migration.downgrade()
    for table in ("submissions", "editor_judge_runs"):
        assert connection.execute(sa.text(f"SELECT status FROM {table}")).scalar_one() == "accepted"
