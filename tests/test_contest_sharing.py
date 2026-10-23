"""Short links are stable locators, never participant authorization."""

import re
from datetime import timedelta
from sqlalchemy import select
from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import ContestRow
from app.services import contest_sharing
from app.services.store import store
from test_contest_visibility import context, client, login


def test_short_link_is_stable_and_contains_no_private_metadata(context):
    c = context
    path = f"/api/operator/contests/{c['cid']}/share-link"
    result = client.post(path, headers=c["owner"])
    assert result.status_code == 200, result.text
    code = result.json()["data"]["code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{11}", code)
    assert client.post(path, headers=c["owner"]).json()["data"]["code"] == code
    result = client.get("/api/public/share/" + code)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["data"] == {"contest_id": c["cid"]}
    assert c["contest"].title not in result.text
    # Knowing the short link does not expose the private contest or its board.
    assert client.get(f"/api/public/contests/{c['cid']}").status_code == 404
    assert client.get(f"/api/contests/{c['cid']}/boards").status_code == 404
    assert (
        client.get(f"/api/public/contests/{c['cid']}", headers=c["member"]).status_code
        == 200
    )


def test_only_staff_of_the_contest_can_create_a_link(context):
    c = context
    path = f"/api/operator/contests/{c['cid']}/share-link"
    assert client.post(path).status_code == 401
    assert client.post(path, headers=c["member"]).status_code == 401
    assert client.post(path, headers=c["outsider"]).status_code == 403
    reviewer = store.upsert_contest_operator(
        c["cid"], "share-reviewer@example.com", "Reviewer", roles=["problem_reviewer"]
    )
    assert client.post(path, headers=login(str(reviewer.email))).status_code == 200


def test_missing_malformed_and_deleted_links_are_not_found(context):
    c = context
    code = contest_sharing.get_or_create(c["cid"])
    for missing in ["missing", "A" * 11, "a" * 120, "../invalid"]:
        assert client.get("/api/public/share/" + missing).status_code == 404
    with SessionLocal() as db:
        row = db.scalar(select(ContestRow).where(ContestRow.contest_id == c["cid"]))
        row.deleted_at = now_utc() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/public/share/" + code).status_code == 404


def test_code_collision_retries_without_replacing_an_existing_link(
    context, monkeypatch
):
    c = context
    first = contest_sharing.get_or_create(c["cid"])
    other = store.create_contest("Other", "Host", "")
    second = contest_sharing.secrets.token_urlsafe(8)
    codes = iter([first, second])
    monkeypatch.setattr(contest_sharing.secrets, "token_urlsafe", lambda _: next(codes))
    assert contest_sharing.get_or_create(other.contest_id) == second
    assert contest_sharing.resolve(first) == c["cid"]


def test_share_link_creation_serializes_across_postgresql_workers(context):
    import pytest
    from concurrent.futures import ThreadPoolExecutor
    from app.database import engine

    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(
            pool.map(lambda _: contest_sharing.get_or_create(context["cid"]), range(4))
        )
    assert len(set(codes)) == 1
    assert contest_sharing.resolve(codes[0]) == context["cid"]


def test_short_link_migration_is_idempotent_and_reversible(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "migrations/versions/0056_contest_share_links.py"
    spec = importlib.util.spec_from_file_location("share_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    with engine.begin() as db:
        db.execute(text("CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            migration.upgrade()
            assert "contest_share_links" in inspect(db).get_table_names()
            db.execute(text("INSERT INTO contests VALUES ('contest')"))
            db.execute(
                text(
                    "INSERT INTO contest_share_links VALUES ('shortCode01', 'contest')"
                )
            )
            migration.downgrade()
            assert "contest_share_links" not in inspect(db).get_table_names()
            assert db.scalar(text("SELECT COUNT(*) FROM contests")) == 1


def test_share_document_is_a_valid_non_indexed_entry_page(context):
    from app.services.seo import build_page_metadata
    code = contest_sharing.get_or_create(context['cid'])
    metadata = build_page_metadata('/share/' + code)
    assert metadata['status_code'] == 200
    assert metadata['title'] == '대회 공유 링크 | ZOJ'
    assert 'noindex' in metadata['robots']
    assert metadata['structured_data'] == []
    assert context['contest'].title not in str(metadata)
