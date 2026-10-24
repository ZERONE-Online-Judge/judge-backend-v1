"""Actual change auditing, scoped operator access and safe division deletion."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('ENABLE_DEMO_SEED', 'true')
os.environ.setdefault('ALLOW_EMPTY_OTP', 'true')
from app.main import app, _audit_changes
from app.models import ContestStatus, now_utc
from app.services.store import store

client = TestClient(app)

def login(email):
    response = TestClient(app).post('/api/auth/general/otp/verify', json={'email':email, 'otp_code':'', 'force_new_session':True})
    assert response.status_code == 200, response.text
    return {'Authorization':'Bearer '+response.json()['data']['operator_session']['access_token']}

@pytest.fixture
def context():
    marker = uuid4().hex
    contest = store.create_contest('운영 개선 '+marker, 'ZOJ', '', now_utc()+timedelta(days=2), status=ContestStatus.OPEN)
    cid = contest.contest_id
    owner = store.upsert_contest_operator(cid, f'owner-{marker}@example.com', '운영자', ['master'])
    viewer = store.upsert_contest_operator(cid, f'viewer-{marker}@example.com', '검수자', ['audit_viewer'])
    manager = store.upsert_contest_operator(cid, f'participants-{marker}@example.com', '참가자 관리자', ['participants_manager'])
    return {'cid':cid, 'contest':contest, 'owner':login(str(owner.email)), 'email':str(owner.email),
            'viewer':login(str(viewer.email)), 'manager':login(str(manager.email)), 'prefix':f'/api/operator/contests/{cid}'}

def last_audit(c, path, method='PATCH'):
    logs = client.get(c['prefix']+'/audit-logs', headers=c['owner']).json()['data']
    return next(log for log in logs if log['path']==path and log['method']==method)

def test_audit_only_records_persisted_changes_and_normalized_dates(context):
    c=context; before=c['contest']; path=c['prefix']+'/settings'
    response=client.patch(path, headers=c['owner'], json={'title':'새 제목', 'organization_name':before.organization_name,
        'start_at':before.start_at.isoformat(), 'freeze_at':before.freeze_at.isoformat(), 'end_at':before.end_at.isoformat(),
        'participant_progress_visible':before.participant_progress_visible})
    assert response.status_code==200,response.text
    log=last_audit(c,path)
    assert log['details']['change_kind']=='updated'
    assert log['details']['changes']==[{'field':'title', 'old':before.title, 'new':'새 제목'}]
    response=client.patch(path, headers=c['owner'], json={'title':'새 제목'})
    assert response.status_code==200
    assert last_audit(c,path)['details'].get('changes',[])==[]

def test_audit_includes_server_side_dependent_changes(context):
    c=context
    store.update_contest_settings(c['cid'], problem_access_after_end='public', editorial_access_after_end='public', mock_judging_enabled=True)
    path=c['prefix']+'/settings'
    response=client.patch(path, headers=c['owner'], json={'problem_access_after_end':'private'})
    assert response.status_code==200,response.text
    fields={change['field'] for change in last_audit(c,path)['details']['changes']}
    assert {'problem_access_after_end','editorial_access_after_end','mock_judging_enabled'} <= fields

def test_failed_request_never_claims_a_change(context):
    c=context;path=c['prefix']+'/settings'
    response=client.patch(path, headers=c['viewer'], json={'title':'forbidden'})
    assert response.status_code==403
    log=last_audit(c,path)
    assert log['details']['change_kind']=='failed'
    assert not log['details'].get('changes')
    assert store.contests[c['cid']].title==c['contest'].title

def test_raw_comparison_precedes_truncation_and_handles_timezones():
    before={'statement':'x'*5000+'old', 'start_at':'2026-01-01T00:00:00Z', 'otp_code':'123456'}
    changes=_audit_changes({'statement':'x'*5000+'new','start_at':'2026-01-01T09:00:00+09:00', 'otp_code':'654321'},before)
    assert [change['field'] for change in changes]==['statement']
    assert '654321' not in str(changes)

def test_operator_access_is_contest_scoped_deduplicated_and_relogin_is_new(context):
    c=context
    for _ in range(3):
        assert client.get(c['prefix']+'/dashboard',headers=c['owner']).status_code==200
    logs=client.get(c['prefix']+'/access-logs',headers=c['viewer']).json()['data']
    visits=[log for log in logs if log['event_type']=='operator_access' and log['email']==c['email']]
    assert len(visits)==1
    assert visits[0]['account_scope']=='staff' and visits[0]['actor_role']=='operator'
    assert c['owner']['Authorization'] not in str(visits)
    assert client.get(c['prefix']+'/dashboard',headers=login(c['email'])).status_code==200
    logs=client.get(c['prefix']+'/access-logs',headers=c['viewer']).json()['data']
    assert len([log for log in logs if log['event_type']=='operator_access' and log['email']==c['email']])==2
    other=store.create_contest('다른 대회','ZOJ','',now_utc()+timedelta(days=2),status=ContestStatus.OPEN)
    assert client.get(f'/api/operator/contests/{other.contest_id}/dashboard',headers=c['viewer']).status_code==403
    assert store.list_access_logs(contest_id=other.contest_id)[0]==[]

def test_unused_division_can_be_removed_only_by_participant_manager(context):
    c=context;division=store.create_contest_division(c['cid'],'unused','삭제 유형')
    path=c['prefix']+'/divisions/'+division.division_id
    assert client.delete(path,headers=c['viewer']).status_code==403
    assert client.delete(path).status_code==401
    assert client.delete(path,headers=c['manager']).status_code==200
    assert division.division_id not in store.divisions
    assert client.delete(path,headers=c['manager']).status_code==404
    logs=client.get(c['prefix']+'/audit-logs',headers=c['owner']).json()['data']
    successful=next(log for log in logs if log['path']==path and log['status_code']==200)
    assert successful['details']['target']['name']=='삭제 유형'
    assert successful['details']['change_kind']=='deleted'

@pytest.mark.parametrize('dependency',['team','problem'])
def test_in_use_divisions_are_not_cascaded(context,dependency):
    c=context;division=store.create_contest_division(c['cid'],'used','사용 유형')
    if dependency=='team':
        store.create_participant_team(c['cid'],division.division_id,'team','leader',f'{uuid4().hex}@example.com',[])
    else:
        store.create_problem(c['cid'],division.division_id,'A','문제','본문',1000,128,{},1)
    response=client.delete(c['prefix']+'/divisions/'+division.division_id,headers=c['manager'])
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='division_in_use'
    assert division.division_id in store.divisions

def test_division_scope_and_running_contest_lock(context):
    c=context
    other=store.create_contest('다른 대회','ZOJ','',now_utc()+timedelta(days=2),status=ContestStatus.OPEN)
    foreign=store.create_contest_division(other.contest_id,'a','foreign')
    assert client.delete(c['prefix']+'/divisions/'+foreign.division_id,headers=c['owner']).status_code==404
    local=store.create_contest_division(c['cid'],'a','local')
    store.update_contest_settings(c['cid'],status=ContestStatus.RUNNING,start_at=now_utc()-timedelta(minutes=1))
    assert client.delete(c['prefix']+'/divisions/'+local.division_id,headers=c['owner']).status_code==409
    assert local.division_id in store.divisions


def test_operator_audit_captures_saved_target_and_only_real_role_identity_changes(context):
    c = context
    email = f'audit-target-{uuid4().hex}@example.com'
    path = c['prefix'] + '/operators'
    payload = {'email': email.upper(), 'display_name': '  검수자 이름  ', 'roles': ['problem_reviewer', 'audit_viewer']}
    response = client.post(path, headers=c['owner'], json=payload)
    assert response.status_code == 200, response.text
    entry = last_audit(c, path, 'POST')['details']
    assert entry['change_kind'] == 'created'
    assert entry['target'] == {'email': email, 'display_name': '검수자 이름', 'roles': ['problem_reviewer', 'audit_viewer']}
    assert not entry.get('changes')

    # POST is an upsert; do not claim another operator was added or roles changed.
    response = client.post(path, headers=c['owner'], json={**payload, 'roles': list(reversed(payload['roles']))})
    assert response.status_code == 200, response.text
    entry = last_audit(c, path, 'POST')['details']
    assert entry['change_kind'] == 'updated'
    assert not entry.get('changes')

    new_email = f'renamed-{uuid4().hex}@example.com'
    update_path = path + '/' + email
    response = client.patch(update_path, headers=c['owner'], json={
        'email': new_email, 'display_name': '검수자 이름', 'roles': ['problem_author']})
    assert response.status_code == 200, response.text
    entry = last_audit(c, update_path)['details']
    assert entry['change_kind'] == 'updated'
    assert {change['field'] for change in entry['changes']} == {'email', 'roles'}
    assert entry['target']['email'] == new_email
    assert entry['target']['roles'] == ['problem_author']

    delete_path = path + '/' + new_email
    assert client.delete(delete_path, headers=c['owner']).status_code == 200
    entry = last_audit(c, delete_path, 'DELETE')['details']
    assert entry['change_kind'] == 'deleted'
    assert entry['target']['display_name'] == '검수자 이름'
    assert entry['target']['roles'] == ['problem_author']


def test_failed_operator_request_does_not_capture_private_saved_identity(context):
    c = context
    path = c['prefix'] + '/operators'
    response = client.post(path, headers=c['viewer'], json={
        'email': c['email'], 'display_name': 'forbidden', 'roles': ['problem_reviewer']})
    assert response.status_code == 403
    entry = last_audit(c, path, 'POST')['details']
    assert entry['change_kind'] == 'failed'
    assert not entry.get('target')
    assert not entry.get('changes')


def test_legacy_verification_labels_are_current_scoped_and_do_not_rewrite_history(context):
    import json
    from app.orm_models import OperationalAuditLogRow
    c = context
    division = store.create_contest_division(c['cid'], 'LABEL', '유형')
    problem = store.create_problem(c['cid'], division.division_id, 'B', '현재 문제명', '비공개 지문', 1000, 128, {}, 1)
    path = c['prefix'] + f'/problems/{problem.problem_id}/verification-tasks'
    own = store.append_operational_audit_log(scope='operator', action='POST raw', method='POST', path=path,
        status_code=200, contest_id=c['cid'], details={'entities': {'problem_id': problem.problem_id}})
    saved = store.append_operational_audit_log(scope='operator', action='POST raw', method='POST', path=path,
        status_code=200, contest_id=c['cid'], details={'target': {'problem_title': '당시 문제명', 'problem_code': 'A'}})
    failed = store.append_operational_audit_log(scope='operator', action='POST raw', method='POST', path=path,
        status_code=403, contest_id=c['cid'], details={})
    other = store.create_contest('other', 'ZOJ', '', now_utc()+timedelta(days=1), status=ContestStatus.OPEN)
    # A forged/mismatched path cannot resolve a name from a different contest.
    foreign = store.append_operational_audit_log(scope='operator', action='POST raw', method='POST',
        path=f'/api/operator/contests/{other.contest_id}/problems/{problem.problem_id}/verification-tasks',
        status_code=200, contest_id=other.contest_id, details={})
    logs, _, _ = store.list_operational_audit_logs()
    details = {entry.operational_audit_log_id: entry.details for entry in logs}
    assert details[own.operational_audit_log_id]['related_target'] == {
        'problem_title': '현재 문제명', 'problem_code': 'B', 'label_source': 'current'}
    assert 'related_target' not in details[saved.operational_audit_log_id]
    assert 'related_target' not in details[failed.operational_audit_log_id]
    assert 'related_target' not in details[foreign.operational_audit_log_id]
    with store._session() as db:
        persisted = json.loads(db.get(OperationalAuditLogRow, own.operational_audit_log_id).details)
    assert 'related_target' not in persisted
    assert '비공개 지문' not in str(details)


def test_new_contests_hide_progress_and_keep_explicit_preferences(context):
    c = context
    path = c['prefix'] + '/settings'
    assert c['contest'].participant_progress_visible is False
    assert c['contest'].mock_judging_progress_visible is False
    enabled = client.patch(path, headers=c['owner'], json={'participant_progress_visible': True})
    assert enabled.status_code == 200
    assert enabled.json()['data']['participant_progress_visible'] is True
    updated = client.patch(path, headers=c['owner'], json={'title': 'Keep saved progress preference'})
    assert updated.status_code == 200
    assert updated.json()['data']['participant_progress_visible'] is True
    other = store.create_contest('Another default contest', 'ZOJ', '')
    assert other.participant_progress_visible is False
    from app.database import engine
    if engine.dialect.name == 'postgresql':
        from sqlalchemy import inspect
        column = next(col for col in inspect(engine).get_columns('contests') if col['name'] == 'participant_progress_visible')
        assert 'false' in str(column['default']).lower()
