"""Only newly created contests opt in; persisted false policies remain false."""
import importlib.util
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient

from app.main import app
from app.models import Contest
from app.orm_models import ContestRow
from app.routers.admin import ContestCreateRequest
from app.services.store import store
from tests.test_presentation_alert_migration import connection
from tests.test_presentation_alert_settings import login


@pytest.fixture
def admin():
    with TestClient(app) as client:
        yield client, login("test3@zoj.com")


def test_new_contests_default_to_counts_in_model_service_and_orm():
    created = store.create_contest("Frozen count default", "ZOJ", "")
    assert created.freeze_submission_count_visible is True
    assert store.get_contest(created.contest_id).freeze_submission_count_visible is True
    assert Contest.model_validate(created.model_dump(exclude={
        "freeze_submission_count_visible",
    })).freeze_submission_count_visible is True
    assert ContestCreateRequest(organization_name="ZOJ").freeze_submission_count_visible is True
    with store._session() as db:
        row = ContestRow(title="ORM default", organization_name="ZOJ", overview="",
            status="draft", start_at=created.start_at, freeze_at=created.freeze_at, end_at=created.end_at)
        db.add(row)
        db.flush()
        assert row.freeze_submission_count_visible is True
        db.rollback()
    assert str(ContestRow.__table__.c.freeze_submission_count_visible.server_default.arg) == "true"


@pytest.mark.parametrize("provided", [None, False, True])
def test_creation_api_uses_new_default_and_honors_explicit_policy(admin, provided):
    client, headers = admin
    body = {"organization_name": "ZOJ", "title": "Creation policy"}
    if provided is not None:
        body["freeze_submission_count_visible"] = provided
    response = client.post("/api/admin/contests", headers=headers, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    expected = provided is not False
    assert data["freeze_submission_count_visible"] is expected
    assert store.get_contest(data["contest_id"]).freeze_submission_count_visible is expected


def test_saved_false_survives_unrelated_patch_and_copy_creation(admin):
    client, headers = admin
    source = store.create_contest("Existing disabled policy", "ZOJ", "",
        freeze_submission_count_visible=False)
    path = f"/api/operator/contests/{source.contest_id}/settings"
    updated = client.patch(path, headers=headers, json={"overview": "Unrelated edit"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["freeze_submission_count_visible"] is False
    # A copy passes the loaded source's explicit configuration through the same
    # creation API; its saved false must take precedence over the new default.
    loaded = store.get_contest(source.contest_id)
    body = {**loaded.model_dump(mode="json"), "title": "Copied disabled policy"}
    copied = client.post("/api/admin/contests", headers=headers, json=body)
    assert copied.status_code == 200, copied.text
    data = copied.json()["data"]
    assert data["contest_id"] != source.contest_id
    assert data["freeze_submission_count_visible"] is False
    assert store.get_contest(data["contest_id"]).freeze_submission_count_visible is False
    assert store.get_contest(source.contest_id).freeze_submission_count_visible is False


def test_creation_api_rejects_null_instead_of_resetting_to_default(admin):
    client, headers = admin
    response = client.post("/api/admin/contests", headers=headers, json={
        "organization_name": "ZOJ", "freeze_submission_count_visible": None,
    })
    assert response.status_code == 422, response.text


@pytest.fixture
def migration():
    path = Path(__file__).parents[1] / "migrations/versions/0067_freeze_count_default.py"
    spec = importlib.util.spec_from_file_location("freeze_count_default_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0066_ioi_subtasks"
    return module


@pytest.mark.parametrize("direction, default", [("upgrade", "true"), ("downgrade", "false")])
def test_postgresql_migration_only_changes_the_insertion_default(migration, monkeypatch, direction, default):
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output,
    })
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, direction)()
    assert output.getvalue().strip() == (
        "ALTER TABLE contests ALTER COLUMN freeze_submission_count_visible "
        f"SET DEFAULT {default};"
    )


def test_migration_keeps_saved_policies_and_related_rows(connection, migration, monkeypatch):
    connection.execute(sa.text("CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY, "
        "freeze_submission_count_visible BOOLEAN NOT NULL DEFAULT false)"))
    connection.execute(sa.text("CREATE TABLE divisions (division_id VARCHAR(36) PRIMARY KEY, "
        "contest_id VARCHAR(36) NOT NULL REFERENCES contests(contest_id))"))
    connection.execute(sa.text("INSERT INTO contests VALUES ('disabled', false), ('enabled', true)"))
    connection.execute(sa.text("INSERT INTO divisions VALUES ('division', 'disabled')"))
    before = connection.execute(sa.text("SELECT * FROM contests ORDER BY contest_id")).all()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    migration.upgrade()
    migration.upgrade()
    assert connection.execute(sa.text("SELECT * FROM contests ORDER BY contest_id")).all() == before
    assert connection.execute(sa.text("SELECT * FROM divisions")).one() == ("division", "disabled")
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('new-default')"))
        assert connection.scalar(sa.text("SELECT freeze_submission_count_visible FROM contests "
            "WHERE contest_id = 'new-default'")) is True
    after = connection.execute(sa.text("SELECT * FROM contests ORDER BY contest_id")).all()
    migration.downgrade()
    migration.downgrade()
    assert connection.execute(sa.text("SELECT * FROM contests ORDER BY contest_id")).all() == after
    connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('old-default')"))
    assert not connection.scalar(sa.text("SELECT freeze_submission_count_visible FROM contests "
        "WHERE contest_id = 'old-default'"))
