"""Anonymous live scoreboard access is independent of archive access."""
from datetime import timedelta
from pathlib import Path
import importlib.util

import pytest
from sqlalchemy import create_engine, text, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models import ContestStatus, now_utc
from app.services.store import store
from test_scoreboard_release_modes import race, client, settings_url


def paths(race):
    base = f"/api/contests/{race['cid']}"
    return [base + '/scoreboard', base + f"/divisions/{race['divisions'][0]}/scoreboard"]


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('archive', ['private', 'participants', 'public'])
@pytest.mark.parametrize('phase', ['before', 'live', 'frozen', 'ended'])
def test_access_matrix(race, enabled, archive, phase):
    now = now_utc()
    changes = dict(status=ContestStatus.RUNNING, scoreboard_public_during_contest=enabled,
                   scoreboard_access_after_end=archive, end_at=now+timedelta(hours=1))
    if phase == 'before':
        changes.update(status=ContestStatus.OPEN, start_at=now+timedelta(hours=1),
                       freeze_at=now+timedelta(hours=2), end_at=now+timedelta(hours=3))
    elif phase == 'live':
        changes['freeze_at'] = now+timedelta(minutes=30)
    elif phase == 'ended':
        changes.update(status=ContestStatus.ENDED, end_at=now-timedelta(minutes=1))
    store.update_contest_settings(race['cid'], **changes)
    allowed = archive == 'public' if phase == 'ended' else enabled and phase in ['live', 'frozen']
    for path in paths(race):
        response = client.get(path)
        assert response.status_code == (200 if allowed else 404), response.text
        if allowed:
            assert response.json()['data']['frozen'] == (phase in ['frozen', 'ended'])


@pytest.mark.parametrize('counts', [False, True])
def test_public_live_view_preserves_freeze_and_internal_results(race, counts):
    store.update_contest_settings(race['cid'], status=ContestStatus.RUNNING,
        end_at=now_utc()+timedelta(hours=1), scoreboard_public_during_contest=True,
        scoreboard_access_after_end='private', freeze_submission_count_visible=counts)
    public = client.get(paths(race)[1]).json()['data']
    green = next(row for row in public['rows'] if row['team_name'] == 'Green')
    score = next(s for s in green['problem_scores'] if s['problem_code'] == 'B')
    assert public['frozen'] is True
    assert green['solved'] == 1 and score['solved'] is False
    assert score['best_status'] is None and score['attempts'] == 0
    assert score.get('pending_attempts', 0) == int(counts)
    assert 'SECRET SOURCE' not in str(public)
    assert all(s.get('best_submission_id') is None for row in public['rows'] for s in row['problem_scores'])
    internal = store.scoreboard_rows(race['cid'], race['divisions'][0], public_view=False)
    assert next(row for row in internal['rows'] if row['team_name'] == 'Green')['solved'] == 2


def test_private_contest_remains_private_and_revocation_covers_wait_endpoints(race):
    store.update_contest_settings(race['cid'], status=ContestStatus.RUNNING,
        end_at=now_utc()+timedelta(hours=1), scoreboard_public_during_contest=True)
    for path in paths(race):
        assert client.get(path).status_code == 200
    store.update_contest_settings(race['cid'], visibility='private')
    for path in paths(race):
        assert client.get(path).status_code == 404
    store.update_contest_settings(race['cid'], visibility='public', scoreboard_public_during_contest=False)
    for path in paths(race):
        for suffix in ['', ':wait?wait_seconds=0']:
            response = client.get(path+suffix)
            assert response.status_code == 404
            assert 'Green' not in response.text


def test_setting_defaults_off_and_round_trips_without_changing_archive_access(race):
    new = store.create_contest('Default off', 'Test', '')
    assert new.scoreboard_public_during_contest is False
    response = client.patch(settings_url(race), headers=race['headers'], json={
        'scoreboard_public_during_contest': True, 'scoreboard_access_after_end': 'private'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['scoreboard_public_during_contest'] is True
    assert response.json()['data']['scoreboard_access_after_end'] == 'private'
    public = client.get(f"/api/public/contests/{race['cid']}")
    assert public.json()['data']['contest']['scoreboard_public_during_contest'] is True
    response = client.patch(settings_url(race), headers=race['headers'], json={'scoreboard_public_during_contest': False})
    assert response.status_code == 200
    assert store.get_contest(race['cid']).scoreboard_public_during_contest is False
    assert client.patch(settings_url(race), json={'scoreboard_public_during_contest': True}).status_code == 401


def test_migration_backfills_false_and_is_idempotent():
    spec = importlib.util.spec_from_file_location('live_public_migration', Path(__file__).parents[1]/'migrations/versions/0059_live_public_scoreboard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY)'))
        connection.execute(text("INSERT INTO contests VALUES ('existing')"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        module.upgrade()
        assert connection.scalar(text('SELECT scoreboard_public_during_contest FROM contests')) == 0
        connection.execute(text("INSERT INTO contests (contest_id) VALUES ('new')"))
        assert connection.scalar(text("SELECT scoreboard_public_during_contest FROM contests WHERE contest_id='new'")) == 0
        assert next(c for c in inspect(connection).get_columns('contests') if c['name']=='scoreboard_public_during_contest')['nullable'] is False
