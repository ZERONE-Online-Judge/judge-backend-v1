"""Hide pre-start participation totals in visitor and participant responses."""
from datetime import timedelta
from pathlib import Path
import importlib.util

import pytest
from sqlalchemy import create_engine, inspect, text
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models import ContestStatus, now_utc
from app.services.store import store
from test_contest_visibility import context, client


@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('phase', ['before', 'running', 'ended'])
def test_totals_policy_for_visitors_and_participants(context, hidden, phase):
    c = context
    now = now_utc()
    start = now + timedelta(hours=1) if phase == 'before' else now - timedelta(hours=2)
    store.update_contest_settings(c['cid'], visibility='public', visibility_after_end='public',
        status=ContestStatus.ENDED if phase == 'ended' else ContestStatus.OPEN,
        start_at=start, end_at=now + timedelta(hours=3) if phase != 'ended' else now-timedelta(hours=1),
        hide_participation_counts_before_start=hidden)
    store.add_team_member(c['cid'], c['team'].participant_team_id, 'Second', 'second-'+c['cid']+'@example.com')
    for headers in [None, c['member'], c['owner']]:
        response = client.get(f"/api/public/contests/{c['cid']}", headers=headers)
        assert response.status_code == 200, response.text
        assert 'no-store' in response.headers['cache-control']
        payload = response.json()['data']
        if hidden and phase == 'before':
            assert 'participant_count' not in payload
            assert 'team_count' not in payload
        else:
            assert payload['participant_count'] == 2
            assert payload['team_count'] == 1
    dashboard = client.get(f"/api/operator/contests/{c['cid']}/dashboard", headers=c['owner'])
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()['data']['participant_count'] == 1  # Existing team-based internal metric.


def test_default_setting_roundtrip_and_exact_start_boundary(context, monkeypatch):
    c = context
    assert store.get_contest(c['cid']).hide_participation_counts_before_start is False
    start = now_utc()+timedelta(hours=1)
    store.update_contest_settings(c['cid'], visibility='public', status=ContestStatus.OPEN, start_at=start, end_at=start+timedelta(hours=2), freeze_at=start+timedelta(hours=1))
    url = f"/api/operator/contests/{c['cid']}/settings"
    result = client.patch(url, headers=c['owner'], json={
        'hide_participation_counts_before_start': True,
    })
    assert result.status_code == 200, result.text
    assert result.json()['data']['hide_participation_counts_before_start'] is True
    from app.routers import public
    monkeypatch.setattr(public, 'now_utc', lambda: start-timedelta(microseconds=1))
    path = f"/api/public/contests/{c['cid']}"
    assert 'team_count' not in client.get(path).json()['data']
    monkeypatch.setattr(public, 'now_utc', lambda: start)
    assert client.get(path).json()['data']['team_count'] == 1
    assert client.patch(url, json={'hide_participation_counts_before_start': False}).status_code == 401


def test_migration_backfills_existing_contests_and_is_reversible():
    spec = importlib.util.spec_from_file_location('hide_counts_migration', Path(__file__).parents[1]/'migrations/versions/0060_hide_prestart_counts.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite:///:memory:')
    field = 'hide_participation_counts_before_start'
    with engine.begin() as db:
        db.execute(text('CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY)'))
        db.execute(text("INSERT INTO contests VALUES ('existing')"))
        migration.op = Operations(MigrationContext.configure(db))
        migration.upgrade()
        migration.upgrade()
        assert db.scalar(text(f'SELECT {field} FROM contests')) == 0
        db.execute(text("INSERT INTO contests (contest_id) VALUES ('new')"))
        assert db.scalar(text(f"SELECT {field} FROM contests WHERE contest_id='new'")) == 0
        assert next(c for c in inspect(db).get_columns('contests') if c['name']==field)['nullable'] is False
        migration.downgrade()
        assert field not in {c['name'] for c in inspect(db).get_columns('contests')}
