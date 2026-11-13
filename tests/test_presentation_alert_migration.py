"""Presentation reminder defaults preserve saved policies and related records."""
import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.schema import CreateSchema

FIELDS = (
    "presentation_freeze_alert_minutes",
    "presentation_end_alert_minutes",
)
DEFAULT_MINUTES = [30, 20, 10, 5]


@pytest.fixture
def migration():
    path = Path(__file__).parents[1] / "migrations/versions/0065_presentation_alert_minutes.py"
    spec = importlib.util.spec_from_file_location("presentation_alert_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0065_presentation_alert_minutes"
    assert module.down_revision == "0064_team_withdrawal"
    return module


@pytest.fixture(params=["sqlite", "postgresql"])
def connection(request):
    if request.param == "sqlite":
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as db:
                db.execute(sa.text("PRAGMA foreign_keys = ON"))
                yield db
        finally:
            engine.dispose()
        return

    url = os.environ.get("EDITOR_JUDGE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set EDITOR_JUDGE_TEST_DATABASE_URL for disposable PostgreSQL migration checks")
    if not url.endswith("/editor_judge_test"):
        pytest.fail("Use a disposable editor_judge_test database")
    engine = sa.create_engine(url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.fail("EDITOR_JUDGE_TEST_DATABASE_URL must use PostgreSQL")
        with engine.connect() as db:
            transaction = db.begin()
            try:
                schema = "presentation_alert_test_" + uuid4().hex
                db.execute(CreateSchema(schema))
                quoted_schema = db.dialect.identifier_preparer.quote(schema)
                db.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}, pg_catalog"))
                yield db
            finally:
                # DDL and data remain in the disposable schema and transaction.
                transaction.rollback()
    finally:
        engine.dispose()


def seed_existing_records(connection, *, existing_freeze_column=False):
    freeze_column = (
        ", presentation_freeze_alert_minutes JSON NOT NULL DEFAULT '[]'"
        if existing_freeze_column else ""
    )
    connection.execute(sa.text(
        "CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY, "
        "title VARCHAR(255) NOT NULL DEFAULT 'Saved title', "
        "scoreboard_release_mode VARCHAR(32) NOT NULL DEFAULT 'resolver'"
        + freeze_column + ")"
    ))
    connection.execute(sa.text(
        "CREATE TABLE divisions (division_id VARCHAR(36) PRIMARY KEY, "
        "contest_id VARCHAR(36) NOT NULL REFERENCES contests(contest_id), "
        "name VARCHAR(255) NOT NULL)"
    ))
    connection.execute(sa.text(
        "INSERT INTO contests (contest_id, scoreboard_release_mode) VALUES ('existing', 'manual')"
    ))
    connection.execute(sa.text(
        "INSERT INTO divisions (division_id, contest_id, name) VALUES ('division', 'existing', 'Keep division')"
    ))


def saved_minutes(connection, contest_id="existing"):
    values = connection.execute(sa.text(
        "SELECT presentation_freeze_alert_minutes, presentation_end_alert_minutes "
        "FROM contests WHERE contest_id = :contest_id"
    ), {"contest_id": contest_id}).one()
    return tuple(json.loads(value) if isinstance(value, str) else value for value in values)


def unrelated_records(connection):
    return (
        connection.execute(sa.text(
            "SELECT contest_id, title, scoreboard_release_mode FROM contests ORDER BY contest_id"
        )).all(),
        connection.execute(sa.text("SELECT * FROM divisions ORDER BY division_id")).all(),
    )


def test_upgrade_backfills_defaults_and_preserves_saved_arrays(connection, migration, monkeypatch):
    seed_existing_records(connection)
    before = unrelated_records(connection)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

    migration.upgrade()
    migration.upgrade()
    assert saved_minutes(connection) == (DEFAULT_MINUTES, DEFAULT_MINUTES)
    assert unrelated_records(connection) == before
    columns = {column["name"]: column for column in sa.inspect(connection).get_columns("contests")}
    assert all(columns[field]["nullable"] is False for field in FIELDS)

    # Raw SQL also receives the default; ORM insertion defaults are insufficient.
    connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('new')"))
    assert saved_minutes(connection, "new") == (DEFAULT_MINUTES, DEFAULT_MINUTES)
    contests = sa.table("contests", sa.column("contest_id", sa.String()),
        *(sa.column(field, sa.JSON()) for field in FIELDS))
    connection.execute(contests.update().where(contests.c.contest_id == "existing").values({
        FIELDS[0]: [45, 3], FIELDS[1]: [],
    }))
    connection.execute(contests.update().where(contests.c.contest_id == "new").values({
        FIELDS[0]: [], FIELDS[1]: [15, 2],
    }))
    migration.upgrade()
    assert saved_minutes(connection) == ([45, 3], [])
    assert saved_minutes(connection, "new") == ([], [15, 2])
    assert unrelated_records(connection)[1] == before[1]

    before_downgrade = unrelated_records(connection)
    migration.downgrade()
    migration.downgrade()
    assert unrelated_records(connection) == before_downgrade
    assert not set(FIELDS).intersection(column["name"] for column in sa.inspect(connection).get_columns("contests"))
    assert sa.inspect(connection).get_foreign_keys("divisions")[0]["referred_table"] == "contests"
    # The referenced table was not rebuilt with its foreign keys disabled.
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(sa.text(
                "INSERT INTO divisions VALUES ('invalid', 'missing', 'Must reject')"
            ))


def test_partial_upgrade_preserves_existing_disabled_field(connection, migration, monkeypatch):
    seed_existing_records(connection, existing_freeze_column=True)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    migration.upgrade()
    migration.upgrade()
    assert saved_minutes(connection) == ([], DEFAULT_MINUTES)
    assert connection.scalar(sa.text(
        "SELECT scoreboard_release_mode FROM contests WHERE contest_id = 'existing'"
    )) == "manual"
