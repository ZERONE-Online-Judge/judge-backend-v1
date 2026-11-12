"""Contest restrictions are enforced for existing tokens and every public fallback."""
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app.main import app
from app.models import ContestResourceAccess, ContestStatus, now_utc
from app.orm_models import OperationalAuditLogRow, TeamSessionRow
from app.services.store import store
from tests.judging_fixtures import ensure_judging_inputs

client = TestClient(app)


def login(email):
    response = TestClient(app).post('/api/auth/general/otp/verify', json={
        'email': email, 'otp_code': '', 'force_new_session': True})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['data']['access_token']}


def participant_login(cid, email):
    response = client.post(f'/api/contests/{cid}/participant-login/otp/verify', json={
        'email': email, 'otp_code': '', 'force_new_session': True})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['data']['access_token']}


@pytest.fixture
def context(monkeypatch, tmp_path):
    from app.settings import settings
    monkeypatch.setattr(settings, 'local_object_storage_root', str(tmp_path / 'objects'))
    stamp = uuid4().hex
    contest = store.create_contest('Restriction test', 'Test', 'Access test', status=ContestStatus.RUNNING,
        start_at=now_utc() - timedelta(hours=1), end_at=now_utc() + timedelta(hours=2))
    cid = contest.contest_id
    store.update_contest_settings(cid, scoreboard_public_during_contest=True,
        problem_access_after_end=ContestResourceAccess.PUBLIC,
        submission_access_after_end=ContestResourceAccess.PUBLIC,
        board_access_after_end=ContestResourceAccess.PUBLIC,
        notice_access_after_end=ContestResourceAccess.PUBLIC)
    owner = store.upsert_contest_operator(cid, f'access-owner-{stamp}@zoj.com', 'Owner')
    viewer = store.upsert_contest_operator(cid, f'access-viewer-{stamp}@zoj.com', 'Viewer', ['problem_reviewer'])
    division = store.create_contest_division(cid, 'A', 'Division A')
    emails = [f'access-{i}-{stamp}@zoj.com' for i in range(3)]
    team = store.create_participant_team(cid, division.division_id, 'Team', 'Leader', emails[0], [('Member', emails[1])])
    other = store.create_participant_team(cid, division.division_id, 'Other', 'Other', emails[2], [])
    problem = store.create_problem(cid, division.division_id, 'A', 'Problem', 'Statement', 1000, 128, {}, 1)
    ensure_judging_inputs(problem.problem_id)
    # General login first; legacy login afterward revokes the general session, so
    # use distinct members to exercise both valid token kinds independently.
    general = login(emails[1])
    legacy = participant_login(cid, emails[0])
    outsider = participant_login(cid, emails[2])
    members = {str(member.email): member for member in team.members}
    return {'cid': cid, 'team': team, 'member': members[emails[1]], 'leader': members[emails[0]],
        'emails': emails, 'problem': problem, 'division': division, 'owner': login(str(owner.email)),
        'viewer': login(str(viewer.email)), 'general': general, 'legacy': legacy, 'other': outsider,
        'base': f'/api/contests/{cid}', 'operator': f'/api/operator/contests/{cid}/participants/{team.participant_team_id}'}


def set_access(c, blocked, *, member=None, reason=' 운영 정책 확인 '):
    path = c['operator'] + (f'/members/{member.team_member_id}' if member else '') + '/access'
    response = client.patch(path, headers=c['owner'], json={'blocked': blocked, 'reason': reason})
    assert response.status_code == 200, response.text
    return response.json()['data']


def assert_blocked(response, scope):
    assert response.status_code == 403, response.text
    error = response.json()['error']
    assert error['code'] == 'participant_access_blocked'
    assert error['details']['scope'] == scope
    assert error['details']['reason'] == '운영 정책 확인'
    assert error['details']['blocked_at']


@pytest.mark.parametrize('token_kind', ['legacy', 'general'])
def test_team_restriction_covers_existing_tokens_reads_writes_public_fallbacks(context, token_kind):
    c = context
    auth = c[token_kind]
    submitted = client.post(c['base'] + f"/problems/{c['problem'].problem_id}/submissions",
        headers=c['legacy'], json={'language': 'cpp17', 'source_code': 'int main() {}'})
    assert submitted.status_code == 200, submitted.text
    sid = submitted.json()['data']['submission_id']
    before_status = store.teams[c['team'].participant_team_id].status
    result = set_access(c, True)
    assert result['access_blocked'] is True
    assert result['status'] == before_status
    assert result['access_blocked_by']
    paths = [f"/api/public/contests/{c['cid']}", c['base'] + '/workspace',
        c['base'] + '/problems', c['base'] + f"/problems/{c['problem'].problem_id}",
        c['base'] + f"/problems/{c['problem'].problem_id}/assets", c['base'] + '/notices',
        c['base'] + '/boards', c['base'] + '/submissions', c['base'] + f'/submissions/{sid}',
        c['base'] + f'/submissions/{sid}/status:wait?wait_seconds=0',
        c['base'] + '/scoreboard', c['base'] + '/scoreboard:wait?wait_seconds=0',
        c['base'] + f"/divisions/{c['division'].division_id}/workspace",
        c['base'] + f"/divisions/{c['division'].division_id}/problems",
        c['base'] + f"/divisions/{c['division'].division_id}/scoreboard",
        c['base'] + f"/divisions/{c['division'].division_id}/scoreboard:wait?wait_seconds=0"]
    paths.append(c['base'] + '/participant-session/me')
    for path in paths:
        assert_blocked(client.get(path, headers=auth), 'team')
    for path, data in [('/boards', {'title': 'Question', 'body': 'Question'}),
        ('/boards/missing/answers', {'body': 'Answer'}),
        (f"/problems/{c['problem'].problem_id}/mock-submissions", {'language': 'cpp17', 'source_code': 'int main(){}'}),
        (f"/problems/{c['problem'].problem_id}/submissions", {'language': 'cpp17', 'source_code': 'int main(){}'})]:
        assert_blocked(client.post(c['base'] + path, headers=auth, json=data), 'team')
    assert client.get('/api/public/contests', headers=auth).status_code == 200
    assert client.get(c['base'] + '/workspace').status_code == 200
    assert client.get(c['base'] + '/workspace', headers=c['other']).status_code == 200
    assert client.get(f"/api/operator/contests/{c['cid']}/participants", headers=c['owner']).status_code == 200
    assert store.get_submission(sid) is not None
    restored = set_access(c, False)
    assert restored['access_blocked_at'] is None
    assert restored['access_block_reason'] is None
    assert restored['access_blocked_by'] is None
    assert client.get(c['base'] + '/workspace', headers=auth).status_code == 200


def test_individual_and_team_restrictions_are_independent(context):
    c = context
    member = set_access(c, True, member=c['member'])
    assert member['access_blocked'] is True
    assert_blocked(client.get(c['base'] + '/workspace', headers=c['general']), 'member')
    assert client.get(c['base'] + '/workspace', headers=c['legacy']).status_code == 200
    set_access(c, True)
    set_access(c, False)
    assert_blocked(client.get(c['base'] + '/workspace', headers=c['general']), 'member')
    assert client.get(c['base'] + '/workspace', headers=c['legacy']).status_code == 200
    set_access(c, False, member=c['member'])
    assert client.get(c['base'] + '/workspace', headers=c['general']).status_code == 200


def test_restrictions_prevent_new_sessions_but_leave_global_identity_and_other_contests(context):
    c = context
    set_access(c, True)
    assert_blocked(client.post(f"/api/auth/general/contests/{c['cid']}/participant-session", headers=c['general']), 'team')
    for action in ('request', 'verify'):
        assert_blocked(client.post(c['base'] + '/participant-login/otp/' + action,
            json={'email': c['emails'][0], 'otp_code': '', 'force_new_session': True}), 'team')
    assert client.get('/api/auth/general/me', headers=c['general']).status_code == 200
    # A new global login is still valid, with the restriction visible in its profile.
    auth = login(c['emails'][1])
    profile = client.get('/api/auth/general/me', headers=auth)
    assert profile.status_code == 200
    assert any(item['team']['access_blocked'] for item in profile.json()['data']['participant_contests'])
    another = store.create_contest('Other contest', 'Test', 'Separate access', status=ContestStatus.RUNNING,
        start_at=now_utc() - timedelta(hours=1), end_at=now_utc() + timedelta(hours=2))
    division = store.create_contest_division(another.contest_id, 'A', 'A')
    store.create_participant_team(another.contest_id, division.division_id, 'Same email', 'Member', c['emails'][1], [])
    issued = client.post(f'/api/auth/general/contests/{another.contest_id}/participant-session', headers=auth)
    assert issued.status_code == 200, issued.text
    child = {'Authorization': 'Bearer ' + issued.json()['data']['access_token']}
    assert client.get(f'/api/contests/{another.contest_id}/workspace', headers=child).status_code == 200


def test_restriction_management_requires_permission_and_scoped_targets_and_audits_changes(context):
    c = context
    path = c['operator'] + '/access'
    assert client.patch(path, json={'blocked': True}).status_code == 401
    assert client.patch(path, headers=c['viewer'], json={'blocked': True}).status_code == 403
    assert client.patch(path, headers=c['owner'], json={'blocked': 'false'}).status_code == 422
    assert client.patch(path, headers=c['owner'], json={'blocked': True, 'reason': 'x' * 1001}).status_code == 422
    assert client.patch(c['operator'] + '/members/missing/access', headers=c['owner'], json={'blocked': True}).status_code == 404
    other_team = store.get_team_by_email(c['cid'], c['emails'][2])
    tampered_member = other_team.members[0].team_member_id
    assert client.patch(c['operator'] + f'/members/{tampered_member}/access', headers=c['owner'], json={'blocked': True}).status_code == 404
    other_contest = store.create_contest('Outside scope', 'Test', 'Other contest')
    wrong_contest_path = c['operator'].replace(c['cid'], other_contest.contest_id) + '/access'
    assert client.patch(wrong_contest_path, headers=login('test3@zoj.com'), json={'blocked': True}).status_code == 404
    set_access(c, True, member=c['member'])
    with store._session() as db:
        log = db.scalars(select(OperationalAuditLogRow).where(
            OperationalAuditLogRow.path == c['operator'] + f"/members/{c['member'].team_member_id}/access",
            OperationalAuditLogRow.status_code == 200).order_by(OperationalAuditLogRow.created_at.desc())).first()
        assert log.actor_email
        import json
        details = json.loads(log.details)
        assert details['body']['reason'] == ' 운영 정책 확인 '
        assert details['target']['name'] == c['member'].name
        assert any(change['field'] == 'access_blocked' and change['old'] is False and change['new'] is True for change in details['changes'])
    # Existing participant sessions are not revoked, permitting immediate recovery.
    with store._session() as db:
        rows = db.scalars(select(TeamSessionRow).where(TeamSessionRow.contest_id == c['cid'])).all()
        assert any(row.revoked_at is None for row in rows)


def test_private_and_ended_public_views_enforce_same_restriction(context):
    c = context
    set_access(c, True)
    from app.models import ContestVisibility
    store.update_contest_settings(c['cid'], visibility=ContestVisibility.PRIVATE)
    assert_blocked(client.get(f"/api/public/contests/{c['cid']}", headers=c['general']), 'team')
    store.update_contest_settings(c['cid'], status=ContestStatus.ENDED,
        end_at=now_utc() - timedelta(seconds=1), visibility_after_end=ContestVisibility.PUBLIC)
    assert_blocked(client.get(c['base'] + '/problems', headers=c['legacy']), 'team')
    assert client.get(c['base'] + '/problems').status_code == 200
    store.update_contest_settings(c['cid'], mock_judging_enabled=True)
    mock = client.post(c['base'] + f"/problems/{c['problem'].problem_id}/mock-submissions",
        json={'language': 'cpp17', 'source_code': 'int main(){}'})
    assert mock.status_code == 200, mock.text
    assert_blocked(client.get(c['base'] + f"/mock-submissions/{mock.json()['data']['submission_id']}/status:wait?wait_seconds=0", headers=c['legacy']), 'team')
    set_access(c, False)
    assert client.get(c['base'] + '/problems', headers=c['legacy']).status_code == 200


def test_private_member_reason_and_actor_remain_operator_only(context):
    c = context
    set_access(c, True, member=c['member'], reason='Private review reason')
    listing = client.get(f"/api/operator/contests/{c['cid']}/participants", headers=c['owner'])
    assert listing.status_code == 200
    operator_team = next(item for item in listing.json()['data'] if item['participant_team_id'] == c['team'].participant_team_id)
    blocked = next(member for member in operator_team['members'] if member['team_member_id'] == c['member'].team_member_id)
    assert blocked['access_block_reason'] == 'Private review reason'
    assert blocked['access_blocked_at'] and blocked['access_blocked_by']
    roster = client.get(c['base'] + '/participant-session/me', headers=c['legacy'])
    assert roster.status_code == 200
    teammate = next(member for member in roster.json()['data']['team']['members'] if member['team_member_id'] == c['member'].team_member_id)
    assert teammate['access_block_reason'] is None
    assert teammate['access_blocked_at'] is None
    assert teammate['access_blocked_by'] is None
    profile = client.get('/api/auth/general/me', headers=c['general'])
    assert profile.status_code == 200
    assert 'Private review reason' not in profile.text
    blocked_response = client.get(c['base'] + '/workspace', headers=c['general'])
    assert blocked_response.status_code == 403
    assert blocked_response.json()['error']['details']['reason'] == 'Private review reason'


def test_real_participant_blocks_do_not_disable_operator_preview(context):
    c = context
    account = store.upsert_contest_operator(c['cid'], f"preview-{uuid4().hex}@zoj.com", 'Preview operator', ['participant_preview'])
    auth = login(str(account.email))
    set_access(c, True)
    response = client.post(f"/api/auth/general/contests/{c['cid']}/participant-preview-session",
        headers=auth, json={'division_id': c['division'].division_id})
    assert response.status_code == 200, response.text
    preview = {'Authorization': 'Bearer ' + response.json()['data']['access_token']}
    assert client.get(c['base'] + '/workspace', headers=preview).status_code == 200


def test_migration_preserves_existing_rows_and_defaults():
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location('participant_access_migration', 'migrations/versions/0063_participant_access.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        for table in ('participant_teams', 'team_members'):
            db.execute(text(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)'))
            db.execute(text(f'INSERT INTO {table} (id) VALUES (1)'))
        migration.op = Operations(MigrationContext.configure(db))
        migration.upgrade()
        migration.upgrade()  # Existing additive installations remain safe.
        for table in ('participant_teams', 'team_members'):
            row = db.execute(text(f'SELECT access_blocked, access_block_reason, access_blocked_at, access_blocked_by FROM {table}')).one()
            assert tuple(row) == (0, None, None, None)
        migration.downgrade()
        assert [column['name'] for column in inspect(db).get_columns('team_members')] == ['id']
