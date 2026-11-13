"""Withdrawal is team-only, reversible, and safe through every reveal stage."""
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.database import engine
from app.models import ContestStatus, now_utc
from app.orm_models import OperationalAuditLogRow, ParticipantTeamRow, ScoreboardReleaseRow, SubmissionRow
from app.services.scoreboard_events import reveal_versions
from app.services.store import store
from tests.contest_fixtures import set_contest_schedule
from test_participant_access import context, login, set_access
from test_performance_controls import cache
from test_scoreboard_release_modes import race, client, public, release, release_url, set_mode


def set_withdrawal(c, withdrawn, *, team_id=None, reason=' 운영진 확인 '):
    path = c['operator'] + '/withdrawal' if 'operator' in c else (
        f"/api/operator/contests/{c['cid']}/participants/{team_id}/withdrawal")
    response = client.patch(path, headers=c.get('owner', c.get('headers')),
        json={'withdrawn': withdrawn, 'reason': reason})
    assert response.status_code == 200, response.text
    return response.json()['data']


def assert_withdrawn(response):
    assert response.status_code == 403, response.text
    error = response.json()['error']
    assert error['code'] == 'participant_access_blocked'
    assert error['details']['scope'] == 'team'
    assert error['details']['withdrawn'] is True
    assert error['details']['reason'] == '운영진 확인'
    assert error['details']['blocked_at']


@pytest.mark.parametrize('token_kind', ['legacy', 'general'])
def test_withdrawal_denies_existing_and_new_sessions_all_team_features(context, token_kind):
    c = context
    before = store.teams[c['team'].participant_team_id]
    item = set_withdrawal(c, True)
    assert item['withdrawn'] is True
    assert item['withdrawal_reason'] == '운영진 확인'
    assert item['withdrawn_at'] and item['withdrawn_by']
    assert item['status'] == before.status
    assert item['access_blocked'] is False  # Only the independent manual flag.
    for suffix in ['/workspace', '/problems', '/submissions', '/boards', '/notices',
            '/scoreboard', '/scoreboard:wait?wait_seconds=0', '/participant-session/me',
            f"/divisions/{c['division'].division_id}/workspace",
            f"/divisions/{c['division'].division_id}/scoreboard"]:
        assert_withdrawn(client.get(c['base'] + suffix, headers=c[token_kind]))
    assert_withdrawn(client.get(f"/api/public/contests/{c['cid']}", headers=c[token_kind]))
    for suffix, payload in [
        (f"/problems/{c['problem'].problem_id}/submissions", {'language': 'cpp17', 'source_code': 'int main(){}'}),
        (f"/problems/{c['problem'].problem_id}/mock-submissions", {'language': 'cpp17', 'source_code': 'int main(){}'}),
        ('/boards', {'title': 'Question', 'body': 'Question'})]:
        assert_withdrawn(client.post(c['base'] + suffix, headers=c[token_kind], json=payload))
    assert_withdrawn(client.post(f"/api/auth/general/contests/{c['cid']}/participant-session", headers=c['general']))
    for action in ['request', 'verify']:
        assert_withdrawn(client.post(c['base'] + '/participant-login/otp/' + action,
            json={'email': c['emails'][0], 'otp_code': '', 'force_new_session': True}))
    assert client.get('/api/auth/general/me', headers=c['general']).status_code == 200
    assert client.get(c['base'] + '/workspace', headers=c['other']).status_code == 200
    restored = set_withdrawal(c, False)
    assert restored['withdrawn'] is False
    assert restored['withdrawal_reason'] is None
    assert restored['withdrawn_at'] is None and restored['withdrawn_by'] is None
    assert client.get(c['base'] + '/workspace', headers=c[token_kind]).status_code == 200


def test_cancelling_withdrawal_preserves_manual_team_and_member_blocks(context):
    c = context
    set_access(c, True)
    set_access(c, True, member=c['member'])
    set_withdrawal(c, True)
    assert_withdrawn(client.get(c['base'] + '/workspace', headers=c['general']))
    set_access(c, False)  # Manual unblock cannot undo withdrawal.
    assert_withdrawn(client.get(c['base'] + '/workspace', headers=c['legacy']))
    set_access(c, True)
    restored = set_withdrawal(c, False)
    assert restored['access_blocked'] is True
    assert next(m for m in restored['members'] if m['team_member_id'] == c['member'].team_member_id)['access_blocked']
    assert client.get(c['base'] + '/workspace', headers=c['legacy']).json()['error']['details']['scope'] == 'team'
    set_access(c, False)
    assert client.get(c['base'] + '/workspace', headers=c['general']).json()['error']['details']['scope'] == 'member'
    assert client.get(c['base'] + '/workspace', headers=c['legacy']).status_code == 200
    # New members inherit team withdrawal without editing their manual setting.
    set_withdrawal(c, True)
    new_member = store.add_team_member(c['cid'], c['team'].participant_team_id,
        'New member', f"new-{c['emails'][0]}")
    assert new_member.access_blocked is False
    assert_withdrawn(client.post(c['base'] + '/participant-login/otp/request', json={'email': str(new_member.email)}))


def test_management_is_team_only_permission_scoped_validated_and_audited(context):
    c = context
    path = c['operator'] + '/withdrawal'
    assert client.patch(path, json={'withdrawn': True}).status_code == 401
    assert client.patch(path, headers=c['viewer'], json={'withdrawn': True}).status_code == 403
    assert client.patch(path, headers=c['owner'], json={'withdrawn': 'false'}).status_code == 422
    assert client.patch(path, headers=c['owner'], json={'withdrawn': True, 'reason': 'x' * 1001}).status_code == 422
    assert client.patch(c['operator'] + f"/members/{c['member'].team_member_id}/withdrawal",
        headers=c['owner'], json={'withdrawn': True}).status_code == 404
    other = store.create_contest('Outside scope', 'Test', '')
    wrong_scope = path.replace(c['cid'], other.contest_id)
    assert client.patch(wrong_scope, headers=login('test3@zoj.com'), json={'withdrawn': True}).status_code == 404
    set_withdrawal(c, True)
    listing = client.get(f"/api/operator/contests/{c['cid']}/participants", headers=c['owner'])
    team = next(t for t in listing.json()['data'] if t['participant_team_id'] == c['team'].participant_team_id)
    assert team['withdrawn'] is True
    with store._session() as db:
        log = db.scalars(select(OperationalAuditLogRow).where(
            OperationalAuditLogRow.path == path, OperationalAuditLogRow.status_code == 200
        ).order_by(OperationalAuditLogRow.created_at.desc())).first()
        details = json.loads(log.details)
        assert log.action == '참가팀 기권 설정' and log.actor_email
        assert details['target']['team_name'] == c['team'].team_name
        assert any(change['field'] == 'withdrawn' and change['old'] is False and change['new'] is True
            for change in details['changes'])
    versions = reveal_versions({c['cid']})
    repeated = set_withdrawal(c, True, reason='Do not overwrite original reason')
    assert repeated['withdrawal_reason'] == '운영진 확인'
    assert reveal_versions({c['cid']}) == versions
    set_withdrawal(c, False)
    with store._session() as db:
        logs = db.scalars(select(OperationalAuditLogRow).where(
            OperationalAuditLogRow.path == path, OperationalAuditLogRow.status_code == 200)).all()
        assert any(any(change['field'] == 'withdrawn' and change['old'] is True and change['new'] is False
            for change in json.loads(log.details).get('changes', [])) for log in logs)


@pytest.mark.parametrize('mode', ['manual', 'manual_all', 'resolver', 'immediate'])
def test_every_board_projection_statistics_freeze_and_restoration(race, mode):
    c = race
    set_mode(c, mode)
    cid, did = c['cid'], c['divisions'][0]
    tid = c['teams'][did]['Blue']
    original = store.scoreboard_rows(cid, did)
    dashboard_before = client.get(f'/api/operator/contests/{cid}/dashboard', headers=c['headers']).json()['data']
    submissions = set(c['submissions'][did].values())
    version = reveal_versions({cid})
    set_withdrawal(c, True, team_id=tid)
    dashboard_after = client.get(f'/api/operator/contests/{cid}/dashboard', headers=c['headers']).json()['data']
    assert dashboard_after['participant_count'] == dashboard_before['participant_count']
    assert dashboard_after['submission_count'] == dashboard_before['submission_count']
    assert reveal_versions({cid}) != version
    for ended, frozen in [(False, False), (False, True), (True, True)]:
        set_contest_schedule(cid, status=ContestStatus.ENDED if ended else ContestStatus.RUNNING,
            end_at=now_utc() - timedelta(minutes=1) if ended else now_utc() + timedelta(hours=1),
            freeze_at=c['start'] + timedelta(hours=1) if frozen else now_utc() + timedelta(minutes=30))
        for division in [None, did]:
            for public_view in [False, True]:
                board = store.scoreboard_rows(cid, division, public_view=public_view)
                assert tid not in {row['team_id'] for row in board['rows']}
                assert all(row['rank'] <= len(board['rows']) for row in board['rows'])
        suffix = f'/divisions/{did}'
        for view in ['operator', 'participant']:
            response = client.get(f'/api/operator/contests/{cid}{suffix}/scoreboard/internal',
                headers=c['headers'], params={'view': view})
            assert response.status_code == 200, response.text
            assert tid not in {row['team_id'] for row in response.json()['data']['rows']}
        board = public(c)
        assert {row['team_name'] for row in board['rows']} == {'Red', 'Green'}
        response = client.get(f'/api/operator/contests/{cid}/scoreboard/presentation', headers=c['headers'])
        section = next(s for s in response.json()['data']['sections'] if s['division']['division_id'] == did)
        assert tid not in {row['team_id'] for row in section['rows']}
    stats = store.scoreboard_rows(cid, did)['problem_stats']
    a, b = stats
    assert a['total_team_count'] == a['attempted_team_count'] == a['accepted_team_count'] == 2
    assert a['total_submissions'] == 2
    assert b['total_team_count'] == 2 and b['total_submissions'] == b['accepted_team_count'] == 1
    assert b['first_accepted_team_name'] == 'Green'
    response = client.get(f'/api/operator/contests/{cid}/problems', headers=c['headers'])
    local = [p for p in response.json()['data'] if p['division_id'] == did]
    assert all(p['total_team_count'] == 2 for p in local)
    assert sorted(p['solved_team_count'] for p in local) == [1, 2]
    if mode != 'immediate':
        release(c, 'all')
    summary = store.scoreboard_release(cid, did)
    assert summary['mode'] == 'all' and summary['total_count'] == summary['revealed_count'] == 2
    assert public(c)['frozen'] is False
    set_withdrawal(c, False, team_id=tid)
    restored = public(c)
    assert restored['frozen'] is False
    assert [row['team_name'] for row in restored['rows']] == ['Blue', 'Green', 'Red']
    assert [row['rank'] for row in restored['rows']] == [1, 2, 3]
    assert store.scoreboard_rows(cid, did)['rows'] == original['rows']
    assert all(store.get_submission(sid) for sid in submissions)


@pytest.mark.parametrize('mode', ['manual', 'resolver'])
def test_partial_reveal_resets_only_changed_division_and_rejects_stale_actions(race, mode):
    c = race
    set_mode(c, mode)
    cid, did = c['cid'], c['divisions'][0]
    other = c['divisions'][1]
    tid = c['teams'][did]['Green']
    release(c, 'start')
    progress = release(c, 'next' if mode == 'resolver' else 'rank', **({} if mode == 'resolver' else {'rank': 2}))
    store.update_scoreboard_release(cid, other, 'all')
    other_board = public(c, other)
    set_withdrawal(c, True, team_id=tid)
    current = store.scoreboard_release(cid, did)
    assert current['mode'] == 'not_started' and current['undo'] is None
    assert current['revision'] == progress['revision'] + 1
    assert public(c, other) == other_board
    stale = client.post(release_url(c), headers=c['headers'],
        json={'action': 'undo', 'expected_revision': progress['revision']})
    assert stale.status_code == 409 and '변경' in stale.json()['error']['message']
    assert client.post(release_url(c), headers=c['headers'],
        json={'action': 'undo', 'expected_revision': current['revision']}).status_code == 409
    restarted = release(c, 'start', expected_revision=current['revision'])
    assert restarted['total_count'] == 2
    release(c, 'all')
    assert tid not in {row['team_id'] for row in public(c)['rows']}
    # A complete announcement stays complete while withdrawal is cancelled.
    set_withdrawal(c, False, team_id=tid)
    final = public(c)
    assert final['release']['mode'] == 'all' and final['release']['total_count'] == 3
    assert [row['rank'] for row in final['rows']] == [1, 2, 3]
    current = store.scoreboard_release(cid, did)
    assert current['undo'] is None


@pytest.mark.parametrize('mode', ['manual', 'resolver'])
def test_withdrawal_from_complete_release_stays_public_without_stale_undo(race, mode):
    c = race
    set_mode(c, mode)
    cid, did = c['cid'], c['divisions'][0]
    release(c, 'all')
    before = store.scoreboard_release(cid, did)
    set_withdrawal(c, True, team_id=c['teams'][did]['Blue'])
    summary = store.scoreboard_release(cid, did)
    assert summary['mode'] == 'all' and summary['total_count'] == 2
    assert summary['revision'] == before['revision'] + 1
    assert summary['undo'] is None
    assert public(c)['frozen'] is False
    assert [row['team_name'] for row in public(c)['rows']] == ['Green', 'Red']
    assert [row['rank'] for row in public(c)['rows']] == [1, 2]
    assert client.post(release_url(c), headers=c['headers'],
        json={'action': 'undo', 'expected_revision': summary['revision']}).status_code == 409


def test_withdrawn_pending_judges_do_not_block_release_and_first_solve_is_recomputed(race):
    c = race
    cid, did = c['cid'], c['divisions'][0]
    tid = c['teams'][did]['Red']
    with store._session() as db:
        row = db.get(SubmissionRow, c['submissions'][did][('Red', 'A')])
        row.status = 'judging'
        db.commit()
    assert client.post(release_url(c), headers=c['headers'], json={'action': 'start'}).status_code == 409
    set_withdrawal(c, True, team_id=tid)
    assert release(c, 'start')['total_count'] == 2
    stat = store.scoreboard_rows(cid, did)['problem_stats'][0]
    assert stat['first_accepted_team_name'] == 'Blue'
    assert stat['total_submissions'] == 2


def test_additive_migration_keeps_existing_rows_and_manual_blocks():
    spec = importlib.util.spec_from_file_location('team_withdrawal_migration', 'migrations/versions/0064_team_withdrawal.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection_engine = create_engine('sqlite://')
    with connection_engine.begin() as db:
        db.execute(text('CREATE TABLE participant_teams (id INTEGER PRIMARY KEY, access_blocked BOOLEAN NOT NULL DEFAULT 0)'))
        db.execute(text('INSERT INTO participant_teams (id, access_blocked) VALUES (1, 1)'))
        migration.op = Operations(MigrationContext.configure(db))
        migration.upgrade()
        migration.upgrade()
        assert tuple(db.execute(text('SELECT access_blocked, withdrawn, withdrawal_reason, withdrawn_at, withdrawn_by FROM participant_teams')).one()) == (1, 0, None, None, None)
        migration.downgrade()
        assert [c['name'] for c in inspect(db).get_columns('participant_teams')] == ['id', 'access_blocked']


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='PostgreSQL row lock regression')
def test_concurrent_withdrawal_and_release_share_contest_lock(race):
    c = race
    cid, did = c['cid'], c['divisions'][0]
    tid = c['teams'][did]['Blue']
    release(c, 'start')
    entered = Event()
    with store._session() as db:
        from app.orm_models import ContestRow
        db.scalar(select(ContestRow).where(ContestRow.contest_id == cid).with_for_update())
        def withdraw():
            entered.set()
            return store.set_participant_withdrawal(cid, tid, withdrawn=True, reason=None, actor_email='test3@zoj.com')
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(withdraw)
            assert entered.wait(timeout=5)
            # A held contest lock keeps the membership change from overtaking
            # an announcement already in progress.
            assert not pending.done()
            db.commit()
            assert pending.result(timeout=10).withdrawn
            current = store.scoreboard_release(cid, did)
            result = pool.submit(store.update_scoreboard_release, cid, did, 'all', None, None, current['revision']).result(timeout=10)
            assert result['mode'] == 'all' and result['total_count'] == 2
    assert tid not in {row['team_id'] for row in public(c)['rows']}


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='PostgreSQL foreign-key lock regression')
def test_submission_and_withdrawal_do_not_invert_contest_and_team_locks(context, monkeypatch):
    """Withdrawal must allow the key-share needed by a concurrent INSERT."""
    from sqlalchemy import event
    import importlib
    store_module = importlib.import_module('app.services.store')
    c = context
    team_locked, allow_insert, withdrawal_contest_locked = Event(), Event(), Event()
    original = store_module.require_active_testcases

    def pause_after_team_lock(db, problem_id):
        original(db, problem_id)
        team_locked.set()
        assert allow_insert.wait(timeout=10)

    def record_contest_lock(conn, cursor, statement, parameters, sql_context, executemany):
        lowered = statement.lower()
        if 'from contests' in lowered and 'for no key update' in lowered:
            withdrawal_contest_locked.set()

    monkeypatch.setattr(store_module, 'require_active_testcases', pause_after_team_lock)
    event.listen(engine, 'after_cursor_execute', record_contest_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(store.create_submission, c['cid'], c['problem'].problem_id,
                c['emails'][0], 'cpp17', 'int main(){}')
            assert team_locked.wait(timeout=5)
            withdrawal = pool.submit(store.set_participant_withdrawal, c['cid'], c['team'].participant_team_id,
                withdrawn=True, reason=None, actor_email='test3@zoj.com')
            try:
                # Withdrawal can hold Contest while waiting for Team, but
                # its NO KEY UPDATE lock must allow the INSERT's FK key-share.
                assert withdrawal_contest_locked.wait(timeout=5)
                assert not withdrawal.done()
            finally:
                allow_insert.set()
            created = submission.result(timeout=10)
            assert withdrawal.result(timeout=10).withdrawn
            assert store.get_submission(created.submission_id) is not None
            assert_withdrawn_reason = client.get(c['base'] + '/workspace', headers=c['legacy'])
            assert assert_withdrawn_reason.status_code == 403
    finally:
        allow_insert.set()
        event.remove(engine, 'after_cursor_execute', record_contest_lock)


@pytest.mark.parametrize('projection', ['division', 'aggregate', 'compressed'])
@pytest.mark.parametrize('redis_enabled', [True, False])
def test_live_serialized_snapshot_obeys_withdrawal_revision_in_same_time_bucket(
    race, cache, monkeypatch, projection, redis_enabled,
):
    import base64
    import gzip
    from app.services import result_cache
    from app.settings import settings

    cid, did = race['cid'], race['divisions'][0]
    tid = race['teams'][did]['Blue']
    set_contest_schedule(cid, status=ContestStatus.RUNNING,
        end_at=now_utc() + timedelta(hours=1))
    monkeypatch.setattr(settings, 'scoreboard_cache_ttl_seconds', 30)
    if not redis_enabled:
        monkeypatch.setattr(settings, 'redis_url', '')
    monkeypatch.setattr(result_cache.time, 'time', lambda: 100.0)
    division = None if projection == 'aggregate' else did
    response_division = store.get_division(cid, did) if projection == 'compressed' else None
    computations = []
    original_compute = store._scoreboard_rows_uncached

    def observe_compute(*args, **kwargs):
        computations.append(1)
        return original_compute(*args, **kwargs)

    monkeypatch.setattr(store, '_scoreboard_rows_uncached', observe_compute)

    def read():
        raw = store.scoreboard_rows(cid, division, public_view=True,
            serialized=True, response_division=response_division)
        if projection == 'compressed':
            assert raw.startswith('gzip:')
            result = json.loads(gzip.decompress(base64.b64decode(raw.removeprefix('gzip:'))))['data']
        else:
            result = json.loads(raw)
        return result

    before = read()
    assert tid in {row['team_id'] for row in before['rows']}
    assert read() == before
    assert len(computations) == 1
    # Ordinary verdict changes still share this bucket, preserving the existing
    # burst protection. Only membership/release policy bypasses that delay.
    sid = race['submissions'][did][('Blue', 'A')]
    with store._session() as db:
        db.get(SubmissionRow, sid).status = 'wrong_answer'
        db.commit()
    assert read() == before
    assert len(computations) == 1
    with store._session() as db:
        db.get(SubmissionRow, sid).status = 'accepted'
        db.commit()
    # Neither the time bucket nor Redis submission generation can carry this
    # policy change. Its revision must invalidate the cached public projection.
    store.set_participant_withdrawal(cid, tid, withdrawn=True, reason=None, actor_email='test3@zoj.com')
    withdrawn = read()
    assert tid not in {row['team_id'] for row in withdrawn['rows']}
    assert read() == withdrawn
    assert len(computations) == 2
    store.set_participant_withdrawal(cid, tid, withdrawn=False, reason=None, actor_email='test3@zoj.com')
    restored = read()
    assert tid in {row['team_id'] for row in restored['rows']}
    assert restored['rows'] == before['rows']
    assert len(computations) == 3
