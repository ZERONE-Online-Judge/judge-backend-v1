import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4
from urllib.parse import urlencode
import pytest
from pydantic import SecretStr
from sqlalchemy import select, func
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.orm_models import EditorImportRow, OperationalAuditLogRow, StaffAccountRow, ProblemRow, ProblemAssetRow
from app.settings import settings
from test_problem_archives import context, export as raw_export, mutate_zip

client=TestClient(app)
KEY='editor-bridge-test-secret-at-least-32-characters'

def export(c):
    def verified(manifest, files):
        body=json.dumps({'format':'zoj-editor-source-v1','verified':True}).encode()
        path='assets/editor-project.json'
        files[path]=body
        manifest['files'].append({'path':path,'size':len(body),'sha256':hashlib.sha256(body).hexdigest()})
        manifest['assets'].append({'id':'asset-9999','file':path,'original_filename':'editor-project.json',
            'mime_type':'application/json','category':'package-files/package-resource','asset_status':'active','created_at':'2026-10-18T00:00:00+00:00'})
    return mutate_zip(raw_export(c),verified)

@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings,'editor_bridge_secret',SecretStr(KEY))

def headers(method,path,email,content=b'',stamp=None,nonce=None):
    ident=base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip('=')
    stamp=str(stamp or int(time.time()));nonce=nonce or uuid4().hex
    sha=hashlib.sha256(content).hexdigest()
    text='\n'.join(['zoj-editor-v1',method,path,stamp,nonce,ident,sha])
    return {'X-Editor-Identity':ident,'X-Editor-Timestamp':stamp,'X-Editor-Nonce':nonce,'X-Editor-Content-Sha256':sha,
            'X-Editor-Signature':hmac.new(KEY.encode(),text.encode(),hashlib.sha256).hexdigest(),'Content-Type':'application/zip'}

def email_for(context,reviewer=False):
    with SessionLocal() as db:
        rows=db.scalars(select(StaffAccountRow).where(StaffAccountRow.contest_scopes.contains(context['cid']))).all()
        return next(r.email for r in rows if ('review-' in r.email)==reviewer)

def target(c,operation=None,code='B'):
    query=urlencode({'division_id':c['division'],'problem_code':code,'operation_id':operation or uuid4().hex})
    return f'/api/editor/contests/{c["cid"]}/imports?{query}'

def test_signed_identity_replay_expiry_tampering_and_scope(context):
    c=context;email=email_for(c);path='/api/editor/contests'
    h=headers('GET',path,email)
    result=client.get(path,headers=h);assert result.status_code==200,result.text
    assert any(x['id']==c['cid'] for x in result.json()['data']['contests'])
    assert client.get(path,headers=h).status_code==401
    assert client.get(path,headers=headers('GET',path,email,stamp=int(time.time())-200)).status_code==401
    assert client.get(path,headers={**headers('GET',path,email),'X-Editor-Identity':base64.urlsafe_b64encode(b'attacker@example.com').decode().rstrip('=')}).status_code==401
    reviewer=email_for(c,True)
    r=client.get(path,headers=headers('GET',path,reviewer));assert r.status_code==200
    assert all(x['id']!=c['cid'] for x in r.json()['data']['contests'])
    content=export(c);path=target(c)
    assert client.post(path,content=content,headers=headers('POST',path,reviewer,content)).status_code==403
    assert client.post(path,content=content,headers=headers('POST',path,email,b'other')).status_code==401


def test_atomic_receipt_retry_revoked_permission_and_audit(context):
    c=context;email=email_for(c);content=export(c);path=target(c)
    first=client.post(path,content=content,headers=headers('POST',path,email,content));assert first.status_code==200,first.text
    pid=first.json()['data']['problem_id']
    retry=client.post(path,content=content,headers=headers('POST',path,email,content));assert retry.status_code==200,retry.text
    assert retry.json()['data']['problem_id']==pid
    changed=path.replace('problem_code=B','problem_code=C')
    assert client.post(changed,content=content,headers=headers('POST',changed,email,content)).status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(EditorImportRow).where(EditorImportRow.problem_id==pid))==1
        assert db.scalar(select(func.count()).select_from(OperationalAuditLogRow).where(OperationalAuditLogRow.action=='Editor 문제 가져오기',OperationalAuditLogRow.contest_id==c['cid']))==1
        account=db.scalar(select(StaffAccountRow).where(StaffAccountRow.email==email));account.contest_scopes='{}';account.contest_roles='{}';db.commit()
    assert client.post(path,content=content,headers=headers('POST',path,email,content)).status_code==403


def test_verified_editor_resource_aliases_import_once(context):
    c = context
    def duplicate_resource(manifest, _):
        resource = next(a for a in manifest['assets'] if a['category'] == 'package-files/package-resource' and a['original_filename'] == 'testlib.h')
        resource['original_filename'] = 'polygon-check.cpp'
        manifest['assets'].append({**resource, 'id': 'asset-9000'})
    content = mutate_zip(export(c), duplicate_resource)
    email = email_for(c)
    path = target(c)
    first = client.post(path, content=content, headers=headers('POST', path, email, content))
    assert first.status_code == 200, first.text
    pid = first.json()['data']['problem_id']
    retry = client.post(path, content=content, headers=headers('POST', path, email, content))
    assert retry.status_code == 200, retry.text
    assert retry.json()['data']['problem_id'] == pid
    with SessionLocal() as db:
        assets = list(db.scalars(select(ProblemAssetRow).where(ProblemAssetRow.problem_id == pid, ProblemAssetRow.original_filename == 'polygon-check.cpp')))
        assert len(assets) == 2
        assert len({a.storage_key for a in assets}) == 2
        assert len({a.sha256 for a in assets}) == 1
        assert db.scalar(select(func.count()).select_from(EditorImportRow).where(EditorImportRow.problem_id == pid)) == 1


def test_transfer_target_list_uses_signed_identity_and_current_contest_role(context):
    c=context; path=f'/api/editor/contests/{c["cid"]}/problems'; email=email_for(c)
    result=client.get(path,headers=headers('GET',path,email))
    assert result.status_code==200,result.text
    with SessionLocal() as db:
        division_id=db.get(ProblemRow,c['pid']).division_id
    assert result.json()['data']['problems']==[{'id':c['pid'],'title':'한글 파일 복원','code':'A','division_id':division_id}]
    assert client.get(path).status_code==401
    assert client.get(path,headers=headers('GET',path,email_for(c,True))).status_code==403
    other=f'/api/editor/contests/{uuid4()}/problems'
    assert client.get(other,headers=headers('GET',other,email)).status_code in {403,404}
