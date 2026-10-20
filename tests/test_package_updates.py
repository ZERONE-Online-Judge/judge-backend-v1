import hashlib
import io
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import (ContestRow, ProblemRow, ProblemAssetRow, SubmissionRow,
                           TestcaseSetRow as CaseSetRow, OperationalAuditLogRow)
from app.services import editor_library as transport
from app.services.storage import object_storage
from app.services.store import store
from test_problem_archives import client, context, mutate_zip
from test_editor_bridge import export, headers, email_for, config
from test_editor_library import configuration, PID, JOB
from contextlib import contextmanager


@pytest.fixture(autouse=True)
def signing_key(configuration, config, monkeypatch):
    from pydantic import SecretStr
    from app.settings import settings
    from test_editor_bridge import KEY
    monkeypatch.setattr(settings, 'editor_bridge_secret', SecretStr(KEY))


def package(c):
    return mutate_zip(export(c), lambda m, _: m['problem'].update(title='수정한 문제', time_limit_ms=2000))


def signed_preview(c, content):
    path=f'/api/editor/contests/{c["cid"]}/problems/{c["pid"]}/package-updates:preview'
    return client.post(path,content=content,headers=headers('POST',path,email_for(c),content))


def signed_apply(c, content, preview, **extra):
    from urllib.parse import urlencode
    values={'target_hash':preview['target_hash'],'reason':'잘못된 테스트 보강','confirm_running':'false',**extra}
    path=f'/api/editor/contests/{c["cid"]}/problems/{c["pid"]}/package-updates?'+urlencode(values)
    return client.post(path,content=content,headers=headers('POST',path,email_for(c),content))


def test_preview_update_preserves_submissions_language_policy_and_backup(context):
    c=context
    with SessionLocal() as db:
        p=db.get(ProblemRow,c['pid']); p.allowed_languages=['cpp17']; db.commit()
        sid=str(uuid4())
        old_time=now_utc()-timedelta(days=1)
        db.add(SubmissionRow(submission_id=sid,contest_id=c['cid'],division_id=p.division_id,
            problem_id=p.problem_id,language='cpp17',source_code='original',status='accepted',
            submitted_at=old_time,runtime_ms=27,memory_kb=100))
        db.commit()
    content=package(c)
    response=signed_preview(c,content); assert response.status_code==200,response.text
    preview=response.json()['data']; assert preview['next_testcase_version']==3 and preview['submission_count']==1
    assert any(v['field']=='title' and v['changed'] for v in preview['changes'])
    assert not any(v['changed'] for v in preview['changes'] if v['field'] in {'statement','editorial'})
    with SessionLocal() as db:
        assert db.get(ProblemRow,c['pid']).title=='한글 파일 복원'
    result=signed_apply(c,content,preview); assert result.status_code==200,result.text
    data=result.json()['data']; assert data['problem']['problem_id']==c['pid']
    with SessionLocal() as db:
        p=db.get(ProblemRow,c['pid']); assert p.title=='수정한 문제' and p.time_limit_ms==2000
        assert p.allowed_languages==['cpp17'] and p.language_resource_limits=={'java8':{'time_limit_ms':3000}}
        sets=list(db.scalars(select(CaseSetRow).where(CaseSetRow.problem_id==c['pid']).order_by(CaseSetRow.version)))
        assert [(s.version,s.is_active) for s in sets]==[(1,False),(2,False),(3,False),(4,True)]
        s=db.get(SubmissionRow,sid); assert s.status=='accepted' and s.runtime_ms==27 and s.source_code=='original'
        assert s.submitted_at.replace(tzinfo=None)==old_time.replace(tzinfo=None)
        assets=db.scalar(select(func.count()).select_from(ProblemAssetRow).where(ProblemAssetRow.problem_id==c['pid']))
    backup=client.get(data['backup_url'],headers=c['owner']); assert backup.status_code==200,backup.text
    import zipfile
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        original=json.loads(archive.read('manifest.json'))
        assert original['problem']['title']=='한글 파일 복원'
    assert client.get(data['backup_url']).status_code==401
    assert signed_apply(c,content,preview).status_code==200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(CaseSetRow).where(CaseSetRow.problem_id==c['pid']))==4
        assert db.scalar(select(func.count()).select_from(ProblemAssetRow).where(ProblemAssetRow.problem_id==c['pid']))==assets
        assert db.scalar(select(func.count()).select_from(OperationalAuditLogRow).where(OperationalAuditLogRow.contest_id==c['cid'],OperationalAuditLogRow.action=='Editor 패키지 업데이트'))==1
    history=client.get(c['prefix']+'/problems/'+c['pid']+'/package-updates',headers=c['owner']).json()['data']
    assert len(history)==1 and history[0]['reason']=='잘못된 테스트 보강'
    assert object_storage.read_bytes(c['keys']['checker'])==c['contents']['checker']


def test_stale_target_cross_contest_and_unverified_archive_are_rejected(context):
    c=context; content=package(c); preview=signed_preview(c,content).json()['data']
    with SessionLocal() as db:
        db.get(ProblemRow,c['pid']).title='다른 운영자 수정';db.commit()
    response=signed_apply(c,content,preview)
    assert response.status_code==409 and response.json()['error']['code']=='package_update_stale'
    from test_problem_archives import export as unverified_export
    response=signed_preview(c,unverified_export(c)); assert response.status_code==409
    assert signed_preview({**c,'pid':str(uuid4())},content).status_code==404


def test_unverified_editor_export_cannot_bypass_verification_via_manual_zip(context):
    c = context
    def remove_verification(manifest, files):
        item = next(a for a in manifest['assets'] if a['original_filename']=='editor-project.json')
        content = json.dumps({'format':'zoj-editor-source-v1','verified':False}).encode()
        files[item['file']] = content
        record = next(f for f in manifest['files'] if f['path']==item['file'])
        record.update(size=len(content),sha256=hashlib.sha256(content).hexdigest())
    content = mutate_zip(export(c),remove_verification)
    from test_problem_archives import upload
    assert upload(c,content,'import').status_code==409


def test_running_contest_requires_explicit_update_confirmation(context):
    c=context; content=package(c)
    with SessionLocal() as db:
        db.get(ContestRow,c['cid']).status='running';db.commit()
    preview=signed_preview(c,content).json()['data']; assert preview['contest_running']
    assert signed_apply(c,content,preview).status_code==409
    assert signed_apply(c,content,preview,confirm_running='true').status_code==200
    notice = next(n for n in store.contest_notices.values() if n.contest_id == c['cid'])
    assert notice.emergency and '잘못된 테스트 보강' in notice.body


def test_operator_package_update_confirms_notice_before_applying(context, monkeypatch):
    from app.services import editor_library as transport
    c = context
    content = package(c)
    with SessionLocal() as db:
        db.get(ContestRow, c['cid']).status = 'running'
        db.commit()
    view = signed_preview(c, content).json()['data']
    sha = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(transport, 'request_json', lambda *args: {'status':'completed', 'package':{'format':'zoj', 'verified':True, 'sha256':sha}})
    @contextmanager
    def download(*args):
        yield io.BytesIO(content)
    monkeypatch.setattr(transport, 'download_package', download)
    body = {'job_id':JOB, 'target_problem_id':c['pid'], 'target_hash':view['target_hash'],
        'archive_sha256':sha, 'reason':'테스트 보강', 'confirm_running':True}
    path = c['prefix'] + f'/editor-problems/{PID}/updates'
    first = client.post(path, headers=c['owner'], json=body)
    assert first.status_code == 409, first.text
    draft = first.json()['error']['details']
    assert '제목' in draft['title'] and '2000' in draft['body']
    assert store.problems[c['pid']].title != '수정한 문제'
    assert not any(n.contest_id == c['cid'] for n in store.contest_notices.values())
    approved = {**draft, 'title':'A번 문제 수정 안내', 'reason':'테스트 보강'}
    saved = client.post(path, headers=c['owner'], json={**body, '_change_notice':approved})
    assert saved.status_code == 200, saved.text
    notice = next(n for n in store.contest_notices.values() if n.contest_id == c['cid'])
    assert notice.title == approved['title'] and notice.emergency
    assert client.post(path, headers=c['owner'], json={**body, '_change_notice':approved}).status_code == 200
    assert len([n for n in store.contest_notices.values() if n.contest_id == c['cid']]) == 1


def test_pending_rejudging_prevents_package_update(context):
    from app.orm_models import RejudgingRow
    c=context;content=package(c);preview=signed_preview(c,content).json()['data']
    with SessionLocal() as db:
        db.add(RejudgingRow(contest_id=c['cid'],problem_id=c['pid'],reason='test',scope={},judge_context={},
            context_hash='a'*64,requested_by_name='Operator',requested_by_email='operator@example.com'))
        db.commit()
    response=signed_apply(c,content,preview)
    assert response.status_code==409 and response.json()['error']['code']=='package_update_rejudging_pending'


@pytest.mark.parametrize('fail_at', [1, 2])
def test_storage_failure_rolls_back_database_and_keeps_old_package(context,monkeypatch,fail_at):
    c=context;content=package(c);preview=signed_preview(c,content).json()['data']
    write=object_storage.write_stream;calls=[]
    def fail_later(*args,**kwargs):
        calls.append(args[0])
        if len(calls)==fail_at:raise OSError('disk unavailable')
        return write(*args,**kwargs)
    monkeypatch.setattr(object_storage,'write_stream',fail_later)
    response=signed_apply(c,content,preview);assert response.status_code==503,response.text
    with SessionLocal() as db:
        assert db.get(ProblemRow,c['pid']).title=='한글 파일 복원'
        assert db.scalar(select(func.count()).select_from(CaseSetRow).where(CaseSetRow.problem_id==c['pid']))==2
    assert signed_preview(c,content).json()['data']['target_hash']==preview['target_hash']
    with pytest.raises(FileNotFoundError):
        object_storage.read_bytes(calls[0])


def test_zoj_library_multi_set_and_update_detects_changed_archive(context,monkeypatch):
    c=context;content=package(c);sha=hashlib.sha256(content).hexdigest();calls=[]
    def remote(method,path,email,values=None):
        calls.append(values)
        return {'status':'completed','revision':3,'package':{'format':'zoj','verified':True,'sha256':sha}}
    @contextmanager
    def download(*args): yield io.BytesIO(content)
    monkeypatch.setattr(transport,'request_json',remote);monkeypatch.setattr(transport,'download_package',download)
    root=c['prefix']+f'/editor-problems/{PID}'
    assert client.post(root+'/packages',headers=c['owner'],json={'testsets':['tests','tle']}).status_code==202
    assert calls[-1]['testsets']==['tests','tle']
    values={'job_id':JOB,'target_problem_id':c['pid']}
    response=client.post(root+'/updates:preview',headers=c['owner'],json=values);assert response.status_code==200,response.text
    p=response.json()['data'];values.update(target_hash=p['target_hash'],archive_sha256='0'*64,reason='test')
    assert client.post(root+'/updates',headers=c['owner'],json=values).status_code==409
    values['archive_sha256']=sha
    response=client.post(root+'/updates',headers=c['owner'],json=values);assert response.status_code==200,response.text
    assert response.json()['data']['problem']['problem_id']==c['pid']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OperationalAuditLogRow).where(
            OperationalAuditLogRow.contest_id==c['cid'], OperationalAuditLogRow.action=='Editor 패키지 업데이트',
            OperationalAuditLogRow.status_code==200))==1
    assert client.post(root+'/updates',headers=c['reviewer'],json=values).status_code==403
