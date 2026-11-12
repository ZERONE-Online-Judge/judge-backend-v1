"""Display credentials must never become operator or participant accounts."""
import os
from datetime import timedelta
from uuid import uuid4

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import PresentationAccountRow, StaffAccountRow, TeamMemberRow, SubmissionRow
from app.services import presentation_access
from app.services.security import decode_session_token, new_session_token
from app.services.store import store

client = TestClient(app)


def staff(email):
    r = TestClient(app).post('/api/auth/general/otp/verify', json={'email': email, 'otp_code': '', 'force_new_session': True})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['data']['access_token']}


def login(email, ip=None):
    return client.post('/api/auth/presentation/login', json={'email': email}, headers={'x-real-ip': ip or str(uuid4())})


def display(cid, session):
    return client.get(f'/api/presentation/contests/{cid}/scoreboard', headers={'Authorization': 'Bearer ' + session['access_token']})


@pytest.fixture
def setup():
    now = now_utc()
    contest = store.create_contest('Presentation access', 'Test', 'Display only',
        start_at=now - timedelta(hours=2), freeze_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=1), status=ContestStatus.RUNNING)
    cid = contest.contest_id
    store.upsert_contest_operator(cid, f'owner-{uuid4().hex}@example.com', 'Owner', ['master'])
    did = store.create_contest_division(cid, 'A', 'Division').division_id
    problem = store.create_problem(cid, did, 'A', 'Problem', 'PRIVATE STATEMENT', 1000, 128, {}, 1)
    team = store.create_participant_team(cid, did, 'Hidden result team', 'Real member', f'{uuid4().hex}@example.com', [])
    with store._session() as db:
        db.add(SubmissionRow(contest_id=cid, division_id=did, problem_id=problem.problem_id,
            participant_team_id=team.participant_team_id, language='cpp17', source_code='PRIVATE SOURCE', status='accepted', submitted_at=now - timedelta(minutes=5)))
        db.commit()
    headers = staff('test3@zoj.com')
    endpoint = f'/api/operator/contests/{cid}/scoreboard/presentation-account'
    account = client.put(endpoint, headers=headers)
    assert account.status_code == 200, account.text
    return cid, did, endpoint, headers, account.json()['data']


def test_short_account_is_isolated_and_management_permission_is_required(setup):
    cid, did, endpoint, headers, account = setup
    alias, domain = account['email'].split('@')
    assert domain == 'score.zoj.kr' and len(alias) == 8 and set(alias) <= set(presentation_access.ALPHABET)
    assert client.get(endpoint, headers=headers).json()['data'] == account
    assert client.get(endpoint, headers=headers).headers['cache-control'] == 'no-store'
    assert client.put(endpoint).status_code == 401
    email = f'viewer-{uuid4().hex}@example.com'
    store.upsert_contest_operator(cid, email, 'Viewer', ['scoreboard_viewer'])
    viewer = staff(email)
    for method in ['get', 'put', 'delete']:
        assert getattr(client, method)(endpoint, headers=viewer).status_code == 403
    manager_email = f'manager-{uuid4().hex}@example.com'
    store.upsert_contest_operator(cid, manager_email, 'Score manager', ['scoreboard_manager'])
    assert client.get(endpoint, headers=staff(manager_email)).status_code == 200
    with store._session() as db:
        assert db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == account['email'])) is None
        assert db.scalar(select(TeamMemberRow).where(TeamMemberRow.email == account['email'])) is None
    for path in ['operators', 'participants']:
        assert account['email'] not in client.get(f'/api/operator/contests/{cid}/{path}', headers=headers).text


def test_passwordless_login_reads_only_its_public_presentation(setup):
    cid, did, endpoint, headers, account = setup
    response = login(account['email'].upper())
    assert response.status_code == 200, response.text
    session = response.json()['data']
    assert 'refresh_token' not in session and 'operator_session' not in session
    response = display(cid, session)
    assert response.status_code == 200
    data = response.json()['data']
    expected = client.get(f'/api/operator/contests/{cid}/scoreboard/presentation', headers=headers).json()['data']
    assert data['sections'] == expected['sections']
    assert all(value == expected['contest'][key] for key, value in data['contest'].items())
    assert data['sections'][0]['frozen'] is True
    assert data['sections'][0]['rows'][0]['solved'] == 0
    assert 'PRIVATE SOURCE' not in response.text and 'PRIVATE STATEMENT' not in response.text
    other = store.create_contest('Other', 'Test', '')
    assert display(other.contest_id, session).status_code == 401
    token = {'Authorization': 'Bearer ' + session['access_token']}
    for path in [f'/operator/contests/{cid}/dashboard', f'/operator/contests/{cid}/scoreboard/internal',
                 f'/operator/contests/{cid}/scoreboard/presentation', f'/operator/contests/{cid}/operators',
                 f'/contests/{cid}/participant-session/me', '/auth/general/me', '/auth/staff/me', '/admin/contests']:
        assert client.get('/api' + path, headers=token).status_code in (401, 403)
    assert client.patch(f'/api/operator/contests/{cid}/settings', headers=token, json={'scoreboard_freeze_mode': 'live'}).status_code == 401
    assert client.put(endpoint, headers=token).status_code == 401
    assert client.post(f'/api/contests/{cid}/problems/x/submissions', headers=token, json={'language':'cpp17', 'source_code':'x'}).status_code in (401, 403)


def test_independent_display_logins_keep_all_screens_authenticated(setup):
    cid, _, _, _, account = setup
    sessions = []
    for agent in ('Presentation screen 1', 'Presentation screen 2', 'Presentation screen 3'):
        response = TestClient(app).post('/api/auth/presentation/login',
            json={'email': account['email']},
            headers={'x-real-ip': str(uuid4()), 'User-Agent': agent})
        assert response.status_code == 200, response.text
        sessions.append(response.json()['data'])
        assert all(display(cid, session).status_code == 200 for session in sessions)
    claims = [decode_session_token(session['access_token'], 'presentation') for session in sessions]
    assert len({claim['jti'] for claim in claims}) == 3
    assert len({claim['credential_id'] for claim in claims}) == 1
    assert all(claim['sub'] == cid for claim in claims)


def test_reissue_revoke_and_account_expiry_invalidate_every_display_session(setup):
    cid, did, endpoint, headers, account = setup
    first = login(account['email']).json()['data']
    second = login(account['email']).json()['data']
    assert display(cid, first).status_code == 200
    assert display(cid, second).status_code == 200
    new = client.put(endpoint, headers=headers).json()['data']
    assert new['email'] != account['email']
    assert display(cid, first).status_code == 401
    assert display(cid, second).status_code == 401
    assert login(account['email']).status_code == 401
    current = [login(new['email']).json()['data'] for _ in range(2)]
    assert all(display(cid, session).status_code == 200 for session in current)
    assert client.delete(endpoint, headers=headers).status_code == 200
    assert all(display(cid, session).status_code == 401 for session in current)
    assert login(new['email']).status_code == 401
    assert client.get(endpoint, headers=headers).json()['data'] is None
    account = client.put(endpoint, headers=headers).json()['data']
    current = [login(account['email']).json()['data'] for _ in range(2)]
    assert all(display(cid, session).status_code == 200 for session in current)
    with store._session() as db:
        db.get(PresentationAccountRow, cid).expires_at = now_utc() - timedelta(seconds=1)
        db.commit()
    assert client.get(endpoint, headers=headers).json()['data']['active'] is False
    assert all(display(cid, session).status_code == 401 for session in current)
    assert login(account['email']).status_code == 401


def test_concurrent_session_policy_keeps_signature_expiry_and_scope_checks(setup):
    cid, _, _, _, account = setup
    valid = login(account['email']).json()['data']
    claims = decode_session_token(valid['access_token'], 'presentation')
    credential = {'credential_id': claims['credential_id']}
    # Even correctly signed tokens need the current credential, scope and expiry.
    rejected = [
        'invalid',
        'A' + valid['access_token'][1:],
        new_session_token('presentation', cid, -1, credential),
        new_session_token('general', cid, 600, credential),
        new_session_token('presentation', 'other-contest', 600, credential),
        new_session_token('presentation', cid, 600, {'credential_id': 'stale'}),
        new_session_token('presentation', cid, 600),
    ]
    for token in rejected:
        assert display(cid, {'access_token': token}).status_code == 401
    assert display(cid, valid).status_code == 200


def test_otp_endpoints_cannot_turn_display_credentials_into_general_accounts(setup):
    email = setup[-1]['email']
    assert client.post('/api/auth/general/otp/request', json={'email':email}).status_code == 401
    assert client.post('/api/auth/general/otp/verify', json={'email':email, 'otp_code':''}).status_code == 401
    assert login('unknown234@score.zoj.kr').status_code == 401
    assert login('test3@zoj.com').status_code == 401


def test_login_throttle_is_shared_and_recovers_after_the_window():
    ip = str(uuid4())
    for _ in range(presentation_access.LOGIN_ATTEMPTS_PER_MINUTE):
        assert login('unknown234@score.zoj.kr', ip).status_code == 401
    response = login('unknown234@score.zoj.kr', ip)
    assert response.status_code == 429
    assert response.json()['error']['details']['retry_after_seconds'] == 60
    assert login('unknown234@score.zoj.kr', str(uuid4())).status_code == 401
    from app.orm_models import PresentationLoginLimitRow
    from app.services.security import token_hash
    with store._session() as db:
        db.get(PresentationLoginLimitRow, token_hash(ip)).window_start = now_utc() - timedelta(minutes=2)
        db.commit()
    assert login('unknown234@score.zoj.kr', ip).status_code == 401


def test_display_obeys_rank_release_and_does_not_leak_hidden_team(setup):
    cid, did, endpoint, headers, account = setup
    session = login(account['email']).json()['data']
    store.update_contest_settings(cid, scoreboard_release_mode="manual", status=ContestStatus.ENDED,
        end_at=now_utc() - timedelta(seconds=1))
    store.update_scoreboard_release(cid, did, 'start')
    response = display(cid, session)
    assert response.status_code == 200
    row = response.json()['data']['sections'][0]['rows'][0]
    assert row['solved'] == 0 and row['submission_count'] == 0
    assert response.json()['data']['sections'][0]['release']['ranks'] == []
    store.update_scoreboard_release(cid, did, 'rank', 1)
    assert 'Hidden result team' in display(cid, session).text


def test_before_start_display_payload_contains_only_countdown_metadata(setup, monkeypatch):
    cid, did, endpoint, headers, account = setup
    other = store.create_contest_division(cid, 'B', 'Other division')
    outside = store.create_contest('Outside contest', 'Test', '')
    outside_division = store.create_contest_division(outside.contest_id, 'C', 'Private other division')
    store.update_contest_settings(cid, start_at=now_utc() + timedelta(days=1),
        freeze_at=now_utc() + timedelta(days=1, hours=2), end_at=now_utc() + timedelta(days=1, hours=3))
    def no_board(*args, **kwargs):
        raise AssertionError('Pre-start displays must not compute scoreboards')
    monkeypatch.setattr(store, 'scoreboard_rows', no_board)
    response = display(cid, login(account['email']).json()['data'])
    assert response.status_code == 200
    assert response.json()['data']['sections'] == []
    assert response.json()['data']['available_divisions'] == [
        {'division_id': did, 'code': 'A', 'name': 'Division'},
        {'division_id': other.division_id, 'code': 'B', 'name': 'Other division'},
    ]
    assert outside_division.division_id not in response.text
    assert 'Hidden result team' not in response.text and 'PRIVATE' not in response.text
    assert 'owner_staff_account_id' not in response.text
