"""Deletion removes access while preserving contest history."""
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import ContestRow, JudgeJobRow, MailQueueItemRow, OperationalAuditLogRow, ParticipantTeamRow, ProblemRow, TeamSessionRow
from app.services.store import store
from tests.contest_fixtures import set_contest_schedule
from tests.judging_fixtures import ensure_judging_inputs

client = TestClient(app)


def login(email, staff=True):
    # Each identity represents a separate browser, not an account switch that
    # intentionally revokes the previous browser's shared login session.
    client.cookies.clear()
    response = client.post('/api/auth/general/otp/verify', json={
        'email': email, 'otp_code': '', 'force_new_session': True})
    assert response.status_code == 200, response.text
    value = response.json()['data']
    token = value['operator_session']['access_token'] if staff else value['access_token']
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def contest():
    now = now_utc()
    row = store.create_contest('Delete ' + uuid4().hex, 'Deletion test', 'Retain history',
        start_at=now - timedelta(days=2), end_at=now - timedelta(days=1),
        freeze_at=now - timedelta(days=1, hours=1), status=ContestStatus.ENDED)
    yield row
    with store._session() as db:
        db.execute(update(JudgeJobRow).where(JudgeJobRow.contest_id == row.contest_id).values(status='failed'))
        db.execute(update(ContestRow).where(ContestRow.contest_id == row.contest_id).values(deleted_at=now_utc()))
        db.commit()


def remove(contest, headers, title=None):
    return client.request('DELETE', '/api/admin/contests/' + contest.contest_id,
        headers=headers, json={'confirm_title': title if title is not None else contest.title})


def test_only_service_master_can_delete_and_the_name_must_match(contest):
    cid = contest.contest_id
    operator_email = uuid4().hex + '@zoj.com'
    store.upsert_contest_operator(cid, operator_email, 'Contest owner')
    assert remove(contest, {}).status_code == 401
    assert remove(contest, login(operator_email)).status_code == 403
    headers = login('test3@zoj.com')
    response = remove(contest, headers, 'Another contest')
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'contest_title_mismatch'
    assert store.get_contest(cid) is not None
    assert remove(contest, headers).status_code == 200
    assert remove(contest, headers).status_code == 404


def test_deleted_contest_disappears_from_lists_urls_and_sessions_but_keeps_history(contest):
    cid = contest.contest_id
    division = store.create_contest_division(cid, 'main', 'Main')
    problem = store.create_problem(cid, division.division_id, 'A', 'Preserved problem', 'Statement', 1000, 256, {}, 1)
    operator_email = uuid4().hex + '@zoj.com'
    store.upsert_contest_operator(cid, operator_email, 'Retained owner')
    other = store.create_contest('Untouched ' + uuid4().hex, 'Other', 'Other')
    store.upsert_contest_operator(other.contest_id, operator_email, 'Other owner')
    operator_headers = login(operator_email)
    participant = uuid4().hex + '@zoj.com'
    team = store.create_participant_team(cid, division.division_id, 'Preserved team', 'Leader', participant, [])
    general_headers = login(participant, staff=False)
    session = store.issue_participant_session_for_general(participant, cid)
    assert session is not None
    token = session[-1]
    assert store.get_participant_by_access_token(cid, token)
    assert store.get_participant_by_general_access_token(cid, general_headers['Authorization'].split()[1])
    store.enqueue_mail('test', participant, 'Retained mail', 'Body', contest_id=cid)
    headers = login('test3@zoj.com')
    assert client.get(f'/api/operator/contests/{cid}/dashboard', headers=operator_headers).status_code == 200
    response = remove(contest, headers)
    assert response.status_code == 200, response.text
    assert response.json()['data']['deleted']
    for path in ['/api/admin/contests', '/api/public/contests', '/api/operator/contests']:
        values = client.get(path, headers=headers).json()['data']
        assert cid not in [row['contest_id'] for row in values]
    for path in [f'/api/public/contests/{cid}', f'/api/contests/{cid}/workspace',
                 f'/api/operator/contests/{cid}/dashboard', f'/api/admin/contests/{cid}/divisions']:
        assert client.get(path, headers=headers).status_code == 404, path
    assert store.get_contest(other.contest_id)
    assert client.get(f'/api/operator/contests/{cid}/dashboard', headers=operator_headers).status_code == 404
    assert client.get(f'/api/operator/contests/{other.contest_id}/dashboard', headers=operator_headers).status_code == 200
    assert cid not in [row['contest_id'] for row in client.get('/api/operator/contests', headers=operator_headers).json()['data']]
    assert not store.get_participant_by_access_token(cid, token)
    assert not store.get_participant_by_general_access_token(cid, general_headers['Authorization'].split()[1])
    assert not store.issue_participant_session_for_general(participant, cid)
    with store._session() as db:
        assert db.get(ContestRow, cid) is None
        archived = db.scalar(select(ContestRow).where(ContestRow.contest_id == cid).execution_options(include_deleted_contests=True))
        assert archived and archived.deleted_at
        assert db.get(ProblemRow, problem.problem_id).title == 'Preserved problem'
        assert db.get(ParticipantTeamRow, team.participant_team_id).team_name == 'Preserved team'
        assert db.scalar(select(TeamSessionRow).where(TeamSessionRow.contest_id == cid)).revoked_at
        mail = db.scalar(select(MailQueueItemRow).where(MailQueueItemRow.contest_id == cid))
        assert mail.status == 'canceled'
        mail_id = mail.mail_queue_id
        audit = db.scalar(select(OperationalAuditLogRow).where(OperationalAuditLogRow.contest_id == cid,
            OperationalAuditLogRow.action == '대회 삭제', OperationalAuditLogRow.status_code == 200))
        assert audit and contest.title in audit.details
    assert store.mark_mail_status(mail_id, 'sending').status == 'canceled'


def test_running_contest_and_outstanding_judging_are_protected(contest):
    cid = contest.contest_id
    headers = login('test3@zoj.com')
    division = store.create_contest_division(cid, 'main', 'Main')
    problem = store.create_problem(cid, division.division_id, 'A', 'Problem', 'Statement', 1000, 256, {}, 1)
    participant = uuid4().hex + '@zoj.com'
    store.create_participant_team(cid, division.division_id, 'Team', 'Leader', participant, [])
    now = now_utc()
    set_contest_schedule(cid, status=ContestStatus.RUNNING, start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=1), freeze_at=now + timedelta(minutes=30))
    response = remove(contest, headers)
    assert response.status_code == 409 and response.json()['error']['code'] == 'contest_running'
    ensure_judging_inputs(problem.problem_id)
    store.create_submission(cid, problem.problem_id, participant, 'cpp17', 'int main(){}')
    store.update_contest_settings(cid, status=ContestStatus.ENDED)
    response = remove(contest, headers)
    assert response.status_code == 409 and response.json()['error']['code'] == 'contest_judging'
    assert store.get_contest(cid)
