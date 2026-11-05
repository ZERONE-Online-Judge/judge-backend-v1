"""Changing the insertion default must preserve saved policies and releases."""
import importlib.util
import os
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.schema import CreateSchema


@pytest.fixture
def migration():
    path = Path(__file__).parents[1] / "migrations/versions/0061_resolver_release_default.py"
    spec = importlib.util.spec_from_file_location("release_default_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_existing_records(connection):
    connection.execute(sa.text(
        "CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY, "
        "scoreboard_release_mode VARCHAR(32) NOT NULL DEFAULT 'manual')"
    ))
    connection.execute(sa.text(
        "CREATE TABLE scoreboard_releases (division_id VARCHAR(36) PRIMARY KEY, "
        "contest_id VARCHAR(36) NOT NULL REFERENCES contests(contest_id) ON DELETE CASCADE, "
        "mode VARCHAR(32), strategy VARCHAR(32), snapshot_rows JSON, "
        "revealed_ranks JSON, revision INTEGER)"
    ))
    modes = ("manual", "immediate", "resolver", "manual_all")
    connection.execute(sa.text(
        "INSERT INTO contests (contest_id, scoreboard_release_mode) "
        "VALUES (:contest_id, :mode)"
    ), [{"contest_id": "saved_" + mode, "mode": mode} for mode in modes])
    connection.execute(sa.text(
        "INSERT INTO scoreboard_releases "
        "(division_id, contest_id, mode, strategy, snapshot_rows, revealed_ranks, revision) "
        "VALUES (:division_id, :contest_id, :mode, :strategy, :snapshot, :ranks, :revision)"
    ), [
        {"division_id": "manual_division", "contest_id": "saved_manual",
         "mode": "partial", "strategy": "manual",
         "snapshot": '[{"team_id":"team-a","rank":2,"solved":3}]',
         "ranks": "[2]", "revision": 4},
        {"division_id": "resolver_division", "contest_id": "saved_resolver",
         "mode": "all", "strategy": "resolver",
         "snapshot": '[{"team_id":"team-b","rank":1,"solved":4}]',
         "ranks": "[1]", "revision": 8},
    ])


def contest_records(connection):
    return connection.execute(sa.text(
        "SELECT contest_id, scoreboard_release_mode FROM contests ORDER BY contest_id"
    )).all()


def release_records(connection):
    return connection.execute(sa.text(
        "SELECT division_id, contest_id, mode, strategy, snapshot_rows, "
        "revealed_ranks, revision FROM scoreboard_releases ORDER BY division_id"
    )).all()


def test_sqlite_migration_leaves_referenced_tables_and_saved_values_untouched(migration, monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys = ON"))
            seed_existing_records(connection)
            contests_before = contest_records(connection)
            releases_before = release_records(connection)
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            statements = []

            def record_statement(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            sa.event.listen(connection, "before_cursor_execute", record_statement)
            try:
                migration.upgrade()
                migration.upgrade()
                migration.downgrade()
                migration.downgrade()
            finally:
                sa.event.remove(connection, "before_cursor_execute", record_statement)

            assert statements == []  # No table rebuild or data update on SQLite.
            assert contest_records(connection) == contests_before
            assert release_records(connection) == releases_before
            assert connection.execute(sa.text("PRAGMA foreign_key_check")).all() == []
            connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('legacy_schema_new')"))
            assert connection.scalar(sa.text(
                "SELECT scoreboard_release_mode FROM contests WHERE contest_id = 'legacy_schema_new'"
            )) == "manual"
    finally:
        engine.dispose()


@pytest.mark.parametrize("direction, default", [("upgrade", "resolver"), ("downgrade", "manual")])
def test_postgresql_migration_only_alters_the_insertion_default(migration, monkeypatch, direction, default):
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output,
    })
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, direction)()
    assert output.getvalue().strip() == (
        "ALTER TABLE contests ALTER COLUMN scoreboard_release_mode "
        f"SET DEFAULT '{default}';"
    )


@pytest.fixture
def postgres_connection():
    url = os.environ.get("EDITOR_JUDGE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires EDITOR_JUDGE_TEST_DATABASE_URL for disposable PostgreSQL schema")
    if not url.endswith("/editor_judge_test"):
        pytest.fail("Use a disposable editor_judge_test database")
    engine = sa.create_engine(url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.fail("EDITOR_JUDGE_TEST_DATABASE_URL must use PostgreSQL")
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                schema = "release_default_test_" + uuid4().hex
                connection.execute(CreateSchema(schema))
                quoted_schema = connection.dialect.identifier_preparer.quote(schema)
                connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}, pg_catalog"))
                yield connection
            finally:
                # Schema creation and all DDL stay in this transaction; rollback
                # removes the disposable schema even if a migration check fails.
                transaction.rollback()
    finally:
        engine.dispose()


def test_postgresql_default_changes_preserve_existing_contests_and_releases(
    migration, monkeypatch, postgres_connection,
):
    connection = postgres_connection
    seed_existing_records(connection)
    contests_before = contest_records(connection)
    releases_before = release_records(connection)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

    migration.upgrade()
    migration.upgrade()
    assert contest_records(connection) == contests_before
    assert release_records(connection) == releases_before
    connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('new_resolver')"))
    assert connection.scalar(sa.text(
        "SELECT scoreboard_release_mode FROM contests WHERE contest_id = 'new_resolver'"
    )) == "resolver"
    contests_after_upgrade = contest_records(connection)

    migration.downgrade()
    migration.downgrade()
    assert contest_records(connection) == contests_after_upgrade
    assert release_records(connection) == releases_before
    connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('new_manual')"))
    assert connection.scalar(sa.text(
        "SELECT scoreboard_release_mode FROM contests WHERE contest_id = 'new_manual'"
    )) == "manual"
