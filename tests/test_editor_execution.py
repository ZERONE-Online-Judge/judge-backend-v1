import base64
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from test_editor_judging import database, short_lock_timeout, headers, client, EMAIL, SECRET, node, snapshot, submit, claim
from app.models import now_utc
from app.orm_models import EditorExecutionTaskRow as Task, JudgeNodeRow, JudgeJobRow, SubmissionRow, ContestRow, ContestDivisionRow, ProblemRow
from app.services import editor_execution as execution
from app.services.editor_protocol import FORMAT, PROTOCOL, KINDS, pack, validate_result
from app.services.store import store
from app.routers import admin
from app.settings import settings
from app.services.errors import AppError
from app.services.storage import object_storage


@pytest.fixture
def execution_db(database, monkeypatch):
    monkeypatch.setattr(execution, 'SessionLocal', database)
    monkeypatch.setattr(execution, '_last_gc', 0)
    return database


def payload(kind='prepare'):
    return {'format': FORMAT, 'operation_id': uuid4().hex, 'job_kind': kind, 'runtime': 'runtime',
            'config': {'command': ['python3', 'sum.py'], 'cpu_ms': 1000, 'memory_mb': 64, 'output_bytes': 1024,
                       'wall_seconds': 8, 'output_file': 'stdout', 'compile_language': None, 'export_files': []},
            'files': {'.input': {'base64': base64.b64encode(b'1 2\n').decode(), 'executable': False},
                      'sum.py': {'base64': base64.b64encode(b'print(sum(map(int,input().split())))\n').decode(), 'executable': False}}}


def enqueue(value=None, *, content=None):
    content = pack(value or payload()) if content is None else content
    path = '/api/editor/judge/tasks'
    return client.post(path, content=content, headers=headers('POST', path, content))


def create_direct(value):
    content = pack(value)
    return execution.create(EMAIL, content, hashlib.sha256(content).hexdigest())


def test_slow_tool_upload_does_not_block_another_request(execution_db, short_lock_timeout, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    write = object_storage.write_bytes
    def slow_first(key, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return write(key, *args, **kwargs)
    monkeypatch.setattr(object_storage, 'write_bytes', slow_first)
    n = ready_node()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create_direct, payload())
        try:
            assert entered.wait(5)
            assert execution.claim_jobs(n, SECRET, 1) == []
            assert pool.submit(create_direct, payload()).result(timeout=5)['status'] == 'waiting'
        finally:
            release.set()
        assert first.result(timeout=5)['status'] == 'waiting'


def test_failed_tool_upload_resumes_same_task(execution_db, monkeypatch):
    value = payload()
    write = object_storage.write_bytes
    def unavailable(*args, **kwargs):
        raise OSError('temporary storage outage')
    monkeypatch.setattr(object_storage, 'write_bytes', unavailable)
    with pytest.raises(AppError) as failure:
        create_direct(value)
    assert failure.value.status_code == 503
    with execution_db() as db:
        task = db.scalar(select(Task))
        assert task.status == 'uploading'
        task_id = task.task_id
    monkeypatch.setattr(object_storage, 'write_bytes', write)
    result = create_direct(value)
    assert result['id'] == task_id and result['status'] == 'waiting'
    with execution_db() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_tool_cancellation_during_upload_cannot_be_undone(execution_db, monkeypatch):
    value = payload()
    write = object_storage.write_bytes
    def cancel_then_write(key, *args, **kwargs):
        execution.cancel_operation(EMAIL, value['operation_id'])
        return write(key, *args, **kwargs)
    monkeypatch.setattr(object_storage, 'write_bytes', cancel_then_write)
    assert create_direct(value)['status'] == 'cancelled'


def ready_node(slots=2, modern=True):
    row = store.provision_node('tool-node', SECRET, slots)
    store.register_node(row.node_name, SECRET, slots, '0.3.0', [PROTOCOL, *(['editor-zoj-runtime-v1'] if modern else [])], 'runtime-digest')
    return row.judge_node_id


def report(job, value=None, secret=SECRET, lease=None):
    value = value or {'status': 'ok', 'exit_code': 0, 'cpu_ms': 17, 'wall_ms': 25, 'memory_kb': 5400,
                      'stderr': '', 'output_base64': base64.b64encode(b'3\n').decode(), 'artifacts': {}}
    return client.post('/api/internal/judge/editor-tasks/'+job['judge_job_id']+'/result',
                       json={'result': value}, headers={'X-Judge-Node-Secret': secret, 'X-Judge-Lease': lease or job['lease_token']})


@pytest.mark.parametrize('kind', sorted(KINDS))
def test_every_editor_kind_uses_the_same_authenticated_queue(execution_db, kind):
    task = enqueue(payload(kind)).json()['data']
    n = ready_node()
    job = claim(n)[0]
    assert job['task_type'] == PROTOCOL and job['judge_job_id'] == task['id']
    assert report(job).status_code == 200
    path = '/api/editor/judge/tasks/'+task['id']
    result = client.get(path, headers=headers('GET', path)).json()['data']
    assert result['status'] == 'completed' and result['result']['output_base64'] == base64.b64encode(b'3\n').decode()
    assert result['node_name'] == 'tool-node' and result['execution_revision'] == 'runtime-digest'
    with execution_db() as db:
        assert db.scalar(select(func.count()).select_from(SubmissionRow)) == 0


@pytest.mark.parametrize('allow_zoj,allow_editor', [(True,False),(False,True),(True,True)])
def test_queue_origin_is_enforced_for_all_editor_work(execution_db, allow_zoj, allow_editor):
    tool_id = enqueue().json()['data']['id']
    judge_id = submit(snapshot()).json()['data']['id']
    n = ready_node(3)
    with execution_db() as db:
        row = db.get(JudgeNodeRow, n)
        row.allow_zoj, row.allow_editor = allow_zoj, allow_editor
        db.add(ContestRow(contest_id='contest',title='Test',organization_name='Test',overview='',status='running',start_at=now_utc(),end_at=now_utc()+timedelta(hours=1),freeze_at=now_utc()))
        db.flush()
        db.add(ContestDivisionRow(division_id='division',contest_id='contest',code='test',name='Test'))
        db.flush()
        db.add(ProblemRow(problem_id='problem',contest_id='contest',division_id='division',problem_code='A',title='Test',statement='',time_limit_ms=1000,memory_limit_mb=128,display_order=1))
        db.flush()
        db.add(SubmissionRow(submission_id='ordinary' , contest_id='contest', division_id='division', problem_id='problem', language='cpp17', source_code='int main(){}', status='waiting'))
        db.flush()
        db.add(JudgeJobRow(judge_job_id='ordinary-job', submission_id='ordinary', contest_id='contest', division_id='division', status='pending', queue_position=1))
        db.commit()
    ids = [j['judge_job_id'] for j in claim(n, 3)]
    assert ('ordinary-job' in ids) == allow_zoj
    assert (tool_id in ids) == allow_editor
    assert (judge_id in ids) == allow_editor
    if allow_zoj:
        assert ids[0] == 'ordinary-job'


def test_unready_old_agent_never_claims_tool_requests(execution_db):
    task = enqueue().json()['data']['id']
    n = node()
    assert claim(n) == []
    assert execution.capabilities()['online_nodes'] == 0
    with execution_db() as db:
        assert db.get(Task, task).status == 'waiting'


def test_slot_capacity_is_shared_and_later_policy_changes_do_not_abort_leases(execution_db):
    n = ready_node(1)
    first, second = enqueue().json()['data']['id'], enqueue().json()['data']['id']
    jobs = claim(n, 100)
    assert len(jobs) == 1
    assert claim(n, 100) == []
    with execution_db() as db:
        row = db.get(JudgeNodeRow, n)
        row.allow_editor = False
        db.commit()
    assert report(jobs[0]).status_code == 200
    assert claim(n, 100) == []
    store.register_node('tool-node', SECRET, 1, '0.2.22', [PROTOCOL])
    store.update_node_heartbeat(n, SECRET, 1, 1, 0, '0.2.22')
    with execution_db() as db:
        assert not db.get(JudgeNodeRow, n).allow_editor
        assert db.get(Task, second).status == 'waiting'


def test_result_retry_ownership_and_lease_constraints(execution_db):
    task = enqueue().json()['data']['id']
    n = ready_node()
    job = claim(n)[0]
    assert report(job, secret='forged').status_code == 403
    assert report(job, lease='wrong').status_code == 409
    assert report(job).status_code == 200
    assert report(job).status_code == 200
    changed = {'status':'ok','exit_code':0,'cpu_ms':0,'wall_ms':0,'memory_kb':0,'stderr':'','output_base64':'','artifacts':{}}
    assert report(job, changed).status_code == 409
    path = '/api/editor/judge/tasks/'+task
    assert client.get(path, headers=headers('GET', path, email='other@example.com')).status_code == 404


def test_cancellation_and_expiry_stop_old_reports_and_requeue(execution_db):
    n = ready_node()
    task = enqueue().json()['data']['id']
    old = claim(n)[0]
    with execution_db() as db:
        db.get(Task, task).leased_at = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds+1)
        db.commit()
    new = claim(n)[0]
    assert old['lease_token'] != new['lease_token']
    assert report(old).status_code == 409
    path = '/api/editor/judge/tasks/'+task
    assert client.delete(path, headers=headers('DELETE', path)).json()['data']['status'] == 'cancelled'
    assert report(new).status_code == 409
    renewal = client.post('/api/internal/judge/jobs/'+task+'/lease:renew',json={'node_secret':SECRET,'lease_token':new['lease_token']})
    assert renewal.status_code == 409


def test_idempotent_submission_rejects_changed_files_and_unsigned_requests(execution_db):
    value=payload()
    # A transport retry resends the same ZIP; repacking changes ZIP timestamps.
    content=pack(value)
    first=enqueue(content=content)
    assert first.status_code == 202, first.text
    retry=enqueue(content=content)
    assert retry.status_code == 202, retry.text
    assert retry.json()['data']['id'] == first.json()['data']['id']
    value['files']['sum.py']['base64']=base64.b64encode(b'print(42)').decode()
    assert enqueue(value).status_code == 409
    assert client.post('/api/editor/judge/tasks',content=pack(payload())).status_code == 401


def test_policy_requires_service_master_and_at_least_one_scope(execution_db, monkeypatch):
    n=ready_node(); path=f'/api/admin/judge/nodes/{n}/workloads'
    assert client.patch(path,json={'allow_zoj':True,'allow_editor':False}).status_code in {401,403}
    monkeypatch.setattr(admin,'require_service_master',lambda request: None)
    assert client.patch(path,json={'allow_zoj':False,'allow_editor':False}).status_code == 422
    assert client.patch(path,json={'allow_zoj':'false','allow_editor':True}).status_code == 422
    assert client.patch(path,json={'allow_zoj':False,'allow_editor':True}).status_code == 200
    with execution_db() as db:
        row=db.get(JudgeNodeRow,n)
        assert row.allow_editor and not row.allow_zoj


@pytest.mark.parametrize('filename',['../evil','/etc/passwd','a/../evil','.editor-job.json','a\\evil','a//b'])
def test_unsafe_file_paths_never_reach_a_node(filename):
    value=payload();value['files'][filename]=value['files'].pop('sum.py')
    with pytest.raises(ValueError):pack(value)


@pytest.mark.parametrize('field,value',[('cpu_ms',0),('cpu_ms',True),('memory_mb',4097),('wall_seconds',float('inf')),('command','python3'),('export_files',['../secret']),('image','arbitrary-image')])
def test_untrusted_resource_and_image_overrides_are_rejected(field,value):
    data=payload();data['config'][field]=value
    with pytest.raises(ValueError):pack(data)


def test_output_artifacts_and_nonfinite_metrics_are_validated():
    limits=payload()['config']
    value={'status':'ok','exit_code':0,'cpu_ms':float('nan'),'output_base64':'','artifacts':{}}
    with pytest.raises(ValueError):validate_result(value,limits)
    value['cpu_ms']=1;value['artifacts']={'../secret':'aGVsbG8='}
    with pytest.raises(ValueError):validate_result(value,limits)
    value['artifacts']={'config.env':'aGVsbG8='}
    with pytest.raises(ValueError):validate_result(value,limits)


def test_revocation_immediately_releases_editor_leases(execution_db, monkeypatch):
    from app.tools import judge_nodes
    monkeypatch.setattr(judge_nodes, 'SessionLocal', execution_db)
    n = ready_node()
    task = enqueue().json()['data']['id']
    job = claim(n)[0]
    judge_nodes.update_credential('tool-node', enabled=False)
    assert report(job).status_code == 403
    with execution_db() as db:
        row = db.get(Task, task)
        assert row.status == 'waiting' and row.lease_token is None and row.assigned_node_id is None


def test_terminal_task_files_expire_without_touching_active_jobs(execution_db):
    n=ready_node()
    done=enqueue().json()['data']['id'];job=claim(n)[0];assert report(job).status_code==200
    pending=enqueue().json()['data']['id']
    with execution_db() as db:
        row=db.get(Task,done);keys=[row.input_key,row.result_key]
        row.completed_at=now_utc()-timedelta(hours=49);db.commit()
    execution._last_gc=0;execution.maintenance(execution_db)
    with execution_db() as db:
        assert db.get(Task,done) is None and db.get(Task,pending).status=='waiting'
    from pathlib import Path
    assert all(not (Path(settings.local_object_storage_root) / key).exists() for key in keys)


def test_postgresql_concurrent_claims_never_oversubscribe_a_node(execution_db):
    if execution_db.kw['bind'].dialect.name!='postgresql':
        pytest.skip('PostgreSQL row locking check')
    from concurrent.futures import ThreadPoolExecutor
    n=ready_node(1)
    for _ in range(4):enqueue()
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches=list(pool.map(lambda _: execution.claim_jobs(n,SECRET,4,execution_db),range(4)))
    assert sum(map(len,batches))==1


def test_signed_long_poll_returns_completed_task(execution_db):
    from concurrent.futures import ThreadPoolExecutor
    import time
    task=enqueue().json()['data']['id'];n=ready_node();job=claim(n)[0]
    path='/api/editor/judge/tasks/'+task+'?wait_seconds=2'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(client.get,path,headers=headers('GET',path))
        time.sleep(.1);assert report(job).status_code==200
        response=future.result(timeout=3)
    assert response.status_code==200 and response.json()['data']['status']=='completed'


def test_deployment_probe_does_not_require_a_user_account(execution_db):
    path='/api/editor/judge/execution-preflight'
    def probe():
        return client.get(path,headers=headers('GET',path,email='editor-runtime-preflight@example.com'))
    assert probe().json()['data']['online_nodes']==0
    ready_node()
    assert probe().json()['data']['online_nodes']==1
    assert client.get(path).status_code==401


def test_public_zoj_availability_excludes_editor_only_nodes(execution_db):
    n=ready_node()
    assert client.get('/api/public/judge-status').json()['data']['active_node_count']==1
    with execution_db() as db:
        db.get(JudgeNodeRow,n).allow_zoj=False;db.commit()
    assert client.get('/api/public/judge-status').json()['data']['active_node_count']==0


def test_finished_task_frees_capacity_even_before_the_next_heartbeat(execution_db):
    n=ready_node(1)
    enqueue();second=enqueue().json()['data']['id']
    first=claim(n)[0]
    store.update_node_heartbeat(n,SECRET,1,0,1,'0.2.22')
    assert report(first).status_code==200
    assert claim(n)[0]['judge_job_id']==second


def test_nonascii_forged_tool_lease_is_rejected_without_server_error(execution_db):
    n=ready_node();task=enqueue().json()['data']['id'];job=claim(n)[0]
    response=client.post('/api/internal/judge/jobs/'+task+'/lease:renew',json={'node_secret':SECRET,'lease_token':'위조된-token'})
    assert response.status_code==409


def test_unicode_node_secret_uses_an_encoded_header(execution_db):
    secret='비밀'*16
    row=store.provision_node('unicode-node',secret,1)
    store.register_node(row.node_name,secret,1,'0.3.0',[PROTOCOL,'editor-zoj-runtime-v1'],'runtime')
    enqueue()
    jobs=client.post(f'/api/internal/judge/nodes/{row.judge_node_id}/assignments:claim',json={'node_secret':secret,'max_count':1}).json()['data']['jobs']
    result={'status':'ok','exit_code':0,'cpu_ms':1,'wall_ms':1,'memory_kb':1,'stderr':'','output_base64':'','artifacts':{}}
    response=client.post('/api/internal/judge/editor-tasks/'+jobs[0]['judge_job_id']+'/result',json={'result':result},headers={
        'X-Judge-Node-Credential':base64.urlsafe_b64encode(secret.encode()).decode(),'X-Judge-Lease':jobs[0]['lease_token']})
    assert response.status_code==200,response.text


def test_cancellation_before_a_late_create_prevents_execution(execution_db):
    value=payload();path='/api/editor/judge/task-operations/'+value['operation_id']
    assert client.delete(path,headers=headers('DELETE',path)).json()['data']['status']=='cancelled'
    task=enqueue(value).json()['data']
    assert task['status']=='cancelled'
    n=ready_node();assert claim(n)==[]


@pytest.mark.parametrize('command', [['python3.11','sum.py'],['pypy3','sum.py'],['kotlinc','Main.kt'],['/usr/local/lib/zoj-jdk/bin/java','-jar','program.jar']])
def test_new_language_tools_wait_for_updated_runtime(execution_db,command):
    value=payload();value['config']['command']=command
    task=enqueue(value).json()['data']
    n=ready_node(modern=False)
    assert execution.claim_jobs(n,SECRET,1)==[]
    with execution_db() as db:
        row=db.get(JudgeNodeRow,n)
        row.execution_capabilities=[PROTOCOL,'editor-zoj-runtime-v1']
        db.commit()
    assert execution.claim_jobs(n,SECRET,1)[0]['judge_job_id']==task['id']
