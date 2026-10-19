"""Contest reassignment must not merge login accounts or transfer authorship."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_staff_email_update import client, context, fresh_email, headers, login
from app.models import now_utc
from app.main import app
from app.orm_models import AccountPasswordRow, IdentityAccountRow, MailQueueItemRow, OperationalAuditLogRow, StaffAccountRow
from app.services.store import store


def replace(c, destination, *, source=None, actor=None, roles=None, name='Replacement'):
    return client.post(c['base']+'/operators/'+(source or str(c['target'].email))+'/account:replace',
        headers=headers(actor or c['actor_session']),
        json={'email':destination,'display_name':name,'roles':roles or ['problem_reviewer']})


def test_existing_logged_in_account_retains_identity_credentials_sessions_and_history(context):
    c=context
    old=str(c['target'].email)
    new=fresh_email('existing')
    with store._session() as db:
        db.add(IdentityAccountRow(email=new,display_name='Existing destination'))
        db.commit()
    destination_session=login(new)
    submission=store.create_operator_test_submission(c['cid'],c['problem'].problem_id,'cpp17','int main(){}',submitted_by_email=old,submitted_by_name='Original author')
    with store._session() as db:
        identity=db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.email==new))
        identity_id, identity_name=identity.account_id,identity.display_name
        for address in (old,new): db.add(AccountPasswordRow(email=address,password_hash='unchanged-'+address,updated_at=1))
        db.commit()
    response=replace(c,' '+new.upper()+' ',name='Do not rename existing account')
    assert response.status_code==200,response.text
    result=response.json()['data']
    assert result['email']==new and result['staff_account_id']!=c['target'].staff_account_id
    assert result['display_name']==identity_name
    with store._session() as db:
        source=db.get(StaffAccountRow,c['target'].staff_account_id)
        assert source.email==old and source.display_name==c['target'].display_name
        assert c['cid'] not in json.loads(source.contest_scopes)
        assert db.get(IdentityAccountRow,identity_id).email==new
        for address in (old,new): assert db.get(AccountPasswordRow,address).password_hash=='unchanged-'+address
        mails=db.scalars(select(MailQueueItemRow).where(MailQueueItemRow.contest_id==c['cid'],MailQueueItemRow.recipient_email==new)).all()
        assert len(mails)==1
        audit=db.scalar(select(OperationalAuditLogRow).where(OperationalAuditLogRow.path==c['base']+'/operators/'+old+'/account:replace'))
        assert audit.status_code==200 and audit.action=='운영자 계정 변경'
        details=json.loads(audit.details)
        assert next(change for change in details['changes'] if change['field']=='email')['old']==old
        assert details['target']['email']==new
    assert client.get(c['base']+'/problems',headers=headers(c['target_session'])).status_code==403
    assert client.get(c['base']+'/problems',headers=headers(destination_session)).status_code==200
    for session,email in [(c['target_session'],old),(destination_session,new)]:
        assert client.get('/api/auth/general/me',headers=headers(session)).json()['data']['account']['email']==email
        with TestClient(app) as fresh_client:
            assert fresh_client.post('/api/auth/general/refresh',json={'refresh_token':session['refresh_token']}).status_code==200
    retained=store.get_submission(submission.submission_id)
    assert retained.submitted_by_email==old and retained.submitted_by_name=='Original author'


def test_other_contests_and_existing_target_staff_profile_are_preserved(context):
    c=context
    other=store.create_contest('Unrelated','Test','',now_utc())
    store.upsert_contest_operator(other.contest_id,str(c['target'].email),'Source profile',['master'])
    destination=fresh_email('other-staff')
    target=store.upsert_contest_operator(other.contest_id,destination,'Destination profile',['audit_viewer'])
    result=replace(c,destination,roles=['notices_manager'],name='Ignored name')
    assert result.status_code==200,result.text
    assert result.json()['data']['staff_account_id']==target.staff_account_id
    assert result.json()['data']['display_name']=='Destination profile'
    assert set(result.json()['data']['contest_scopes'])=={c['cid']}
    with store._session() as db:
        a=db.get(StaffAccountRow,c['target'].staff_account_id)
        b=db.get(StaffAccountRow,target.staff_account_id)
        assert json.loads(a.contest_roles)=={other.contest_id:['owner']}
        assert json.loads(b.contest_roles)=={other.contest_id:['audit_viewer'],c['cid']:['notices_manager']}


def test_unregistered_destination_gets_only_the_selected_contest_assignment(context):
    c=context
    new=fresh_email('invited')
    result=replace(c,new,name='New operator',roles=['participant_preview'])
    assert result.status_code==200,result.text
    assert result.json()['data']['display_name']=='New operator'
    assert result.json()['data']['contest_roles']=={c['cid']:['participant_preview']}


@pytest.mark.parametrize('reason',['assigned','participant','service_master','same'])
def test_invalid_destination_keeps_both_accounts_unchanged(context,reason):
    c=context
    new=fresh_email('occupied')
    code,status='operator_account_already_assigned',409
    if reason=='assigned': store.upsert_contest_operator(c['cid'],new,'Existing',['notices_manager'])
    elif reason=='participant':
        store.create_participant_team(c['cid'],c['division'].division_id,'Participant','Participant',new,[])
        code,status='operator_account_participant',422
    elif reason=='service_master': new='test3@zoj.com';code='service_master_operator_immutable'
    else: new=str(c['target'].email);code,status='operator_account_unchanged',422
    with store._session() as db:
        before={r.staff_account_id:(r.email,r.display_name,r.contest_scopes,r.contest_roles) for r in db.scalars(select(StaffAccountRow))}
    result=replace(c,new)
    assert result.status_code==status,result.text
    assert result.json()['error']['code']==code
    with store._session() as db:
        assert before=={r.staff_account_id:(r.email,r.display_name,r.contest_scopes,r.contest_roles) for r in db.scalars(select(StaffAccountRow))}


def test_owner_cannot_be_replaced_and_master_requires_master_actor(context):
    c=context
    owner=next(a for a in store.contest_operator_accounts(c['cid']) if c['cid'] in a.protected_master_contests)
    owner_session=login(str(owner.email))
    denied=replace(c,fresh_email(),source=str(owner.email),actor=owner_session,roles=['master'])
    assert denied.status_code==409 and denied.json()['error']['code']=='contest_owner_immutable'
    master=store.upsert_contest_operator(c['cid'],fresh_email('master'),'Master',['master'])
    assert replace(c,fresh_email(),source=str(master.email)).status_code==403
    assert replace(c,fresh_email(),roles=['master']).status_code==403
    allowed=replace(c,fresh_email(),source=str(master.email),actor=owner_session,roles=['master'])
    assert allowed.status_code==200,allowed.text


def test_source_self_reassignment_preserves_general_login_and_current_audit_actor(context):
    c=context
    old=str(c['actor'].email)
    result=replace(c,fresh_email('successor'),source=old,roles=['staff_manager'])
    assert result.status_code==200,result.text
    assert client.get('/api/auth/general/me',headers=headers(c['actor_session'])).status_code==200
    assert client.get(c['base']+'/operators',headers=headers(c['actor_session'])).status_code==403
    with store._session() as db:
        audit=db.scalar(select(OperationalAuditLogRow).where(OperationalAuditLogRow.path==c['base']+'/operators/'+old+'/account:replace'))
        assert audit.actor_email==old and audit.status_code==200
