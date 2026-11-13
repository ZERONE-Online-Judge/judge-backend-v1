"""Undo must restore visible results, survive reloads, and reject stale actions."""
from datetime import timedelta
from uuid import uuid4

import pytest

from test_scoreboard_release_modes import (
    race, client, release, release_url, public, set_mode, visible_scores,
    assert_presentation_matches, login,
)
from app.models import ContestStatus, now_utc
from app.orm_models import ScoreboardReleaseRow, SubmissionRow
from app.services.store import store
from tests.contest_fixtures import set_contest_schedule


def undo(race, **kwargs):
    current = client.get(release_url(race), headers=race['headers']).json()['data']
    return release(race, 'undo', expected_revision=current['revision'], **kwargs)


def test_manual_undo_rank_all_and_start_restores_exact_previous_boards(race):
    before = public(race)
    other = public(race, race['divisions'][1])
    started = release(race, 'start', expected_revision=0)
    hidden = public(race)
    assert started['undo'] == {'action': 'start'}
    second = release(race, 'rank', rank=2, expected_revision=started['revision'])
    partly = public(race)
    assert second['undo'] == {'action': 'rank', 'rank': 2}
    all_result = release(race, 'all', expected_revision=second['revision'])
    assert all_result['undo'] == {'action': 'all'}
    assert undo(race)['revision'] > all_result['revision']
    assert public(race) == partly
    undo(race)
    assert public(race) == hidden
    stopped = undo(race)
    assert stopped['undo'] is None
    assert stopped['mode'] == 'not_started'
    assert public(race) == before
    assert public(race, race['divisions'][1]) == other
    assert_presentation_matches(race, before)
    assert 'undo' not in before['release']
    assert 'revision' not in before['release']
    # Undoing start allows changing strategy and making a fresh snapshot.
    set_mode(race, 'resolver')
    resumed = release(race, 'start', expected_revision=stopped['revision'])
    assert resumed['strategy'] == 'resolver'


@pytest.mark.parametrize('strategy', ['manual', 'resolver'])
def test_direct_all_can_return_to_before_start_without_undo_history_leaks(race, strategy):
    set_mode(race, strategy)
    before = public(race)
    release(race, 'all')
    result = public(race)
    assert result['release']['mode'] == 'all'
    assert 'undo_history' not in str(result)
    undo(race)
    assert public(race) == before


@pytest.mark.parametrize('last_action', ['next', 'all'])
def test_resolver_undo_restores_pending_verdicts_ranks_and_original_snapshot(race, last_action):
    set_mode(race, 'resolver')
    did = race['divisions'][0]
    release(race, 'start')
    initial = public(race)
    release(race, 'next')
    first = public(race)
    release(race, last_action)
    final = public(race)
    assert final['release']['mode'] == 'all'
    with store._session() as db:
        # Rejudging after the snapshot must not change the inverse operation.
        db.get(SubmissionRow, race['submissions'][did][('Blue', 'B')]).status = 'wrong_answer'
        db.commit()
    undone = undo(race)
    assert undone['resolver']['step'] == 1
    assert public(race) == first
    assert_presentation_matches(race, first)
    undo(race)
    assert public(race) == initial
    release(race, 'next')
    release(race, 'next')
    assert public(race)["rows"] == final["rows"]
    assert public(race)["release"]["mode"] == "all"
    # Advancing keeps only the affected team/event, not a full board per step.
    with store._session() as db:
        history = db.get(ScoreboardReleaseRow, did).undo_history
        assert 'resolver_delta' in history[-1]
        assert 'resolver_state' not in history[-1]


def test_stale_or_duplicate_undo_and_forward_requests_cannot_skip_steps(race):
    set_mode(race, 'resolver')
    started = release(race, 'start')
    stepped = release(race, 'next', expected_step=0, expected_revision=started['revision'])
    first_undo = undo(race)
    before = public(race)
    for body in [
        {'action': 'undo', 'expected_revision': stepped['revision']},
        {'action': 'undo'},
        {'action': 'next', 'expected_step': 0, 'expected_revision': started['revision']},
        {'action': 'all', 'expected_revision': stepped['revision']},
    ]:
        response = client.post(release_url(race), headers=race['headers'], json=body)
        assert response.status_code == 409, response.text
        assert public(race) == before
    assert first_undo['revision'] > stepped['revision']
    # Repeating an already applied action creates no spurious undo step.
    assert release(race, 'start') == first_undo


def test_legacy_release_has_explicit_reset_instead_of_invented_history(race):
    set_mode(race, 'resolver')
    before = public(race)
    release(race, 'start')
    release(race, 'next')
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, race['divisions'][0])
        saved.undo_history = None
        db.commit()
    legacy = client.get(release_url(race), headers=race['headers']).json()['data']
    assert legacy['undo'] == {'action': 'legacy'}
    release(race, 'all')
    assert undo(race)['undo'] == {'action': 'legacy'}
    assert undo(race)['mode'] == 'not_started'
    assert public(race) == before


def test_immediate_undo_holds_freeze_until_explicit_resume_and_keeps_live_judging(race):
    frozen = public(race)
    other = race['divisions'][1]
    set_mode(race, 'immediate')
    current = client.get(release_url(race), headers=race['headers']).json()['data']
    assert current['undo'] == {'action': 'automatic'}
    held = undo(race)
    assert held['mode'] == 'partial'
    assert held['undo'] is None
    assert held['total_count'] == 3
    assert public(race)['frozen'] is True
    assert visible_scores(public(race)) == visible_scores(frozen)
    assert public(race, other)['frozen'] is False
    assert_presentation_matches(race, public(race))
    # A live freeze override must not silently cancel the undo hold.
    store.update_contest_settings(race['cid'], scoreboard_freeze_mode='live')
    assert visible_scores(public(race)) == visible_scores(frozen)
    did = race['divisions'][0]
    with store._session() as db:
        db.get(SubmissionRow, race['submissions'][did][('Blue', 'B')]).status = 'wrong_answer'
        db.commit()
    resumed = release(race, 'all', expected_revision=held['revision'])
    assert resumed['mode'] == 'all'
    assert resumed['revealed_count'] == 3
    assert public(race)['frozen'] is False
    assert visible_scores(public(race))[0][0] == 'Green'
    with store._session() as db:
        db.get(SubmissionRow, race['submissions'][did][('Blue', 'B')]).status = 'accepted'
        db.commit()
    assert visible_scores(public(race))[0][0] == 'Blue'
    undo(race)
    assert visible_scores(public(race)) == visible_scores(frozen)


def test_immediate_undo_after_undoing_manual_start_and_changing_strategy(race):
    release(race, 'start')
    stopped = undo(race)
    set_mode(race, 'immediate')
    assert public(race)['release']['mode'] == 'all'
    current = client.get(release_url(race), headers=race['headers']).json()['data']
    assert current['undo'] == {'action': 'automatic'}
    assert current['revision'] == stopped['revision']
    assert undo(race)['mode'] == 'partial'


def test_undo_requires_management_permission_and_ended_contest(race):
    store.upsert_contest_operator(race['cid'], f'owner-{uuid4().hex}@zoj.com', 'Owner', ['master'])
    account = store.upsert_contest_operator(race['cid'], f'viewer-{uuid4().hex}@zoj.com', 'Viewer', ['scoreboard_viewer'])
    viewer = login(str(account.email))
    started = release(race, 'start')
    body = {'action': 'undo', 'expected_revision': started['revision']}
    assert client.post(release_url(race), json=body).status_code == 401
    assert client.post(release_url(race), headers=viewer, json=body).status_code == 403
    set_contest_schedule(race['cid'], status=ContestStatus.RUNNING, end_at=now_utc()+timedelta(hours=1))
    assert client.post(release_url(race), headers=race['headers'], json=body).status_code == 409


def test_undo_migration_is_idempotent_and_preserves_existing_releases():
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / 'migrations/versions/0032_scoreboard_release_undo.py'
    spec = importlib.util.spec_from_file_location('undo_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE scoreboard_releases (division_id TEXT PRIMARY KEY, mode TEXT, revealed_ranks JSON)'))
        connection.execute(sa.text("INSERT INTO scoreboard_releases VALUES ('d', 'all', '[1,2,3]')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        result = connection.execute(sa.text('SELECT mode, revealed_ranks, undo_history, revision FROM scoreboard_releases')).one()
        assert tuple(result) == ('all', '[1,2,3]', None, 0)
        migration.downgrade()
        assert connection.scalar(sa.text('SELECT revealed_ranks FROM scoreboard_releases')) == '[1,2,3]'
