"""Registered verification runs retain every test without altering contest semantics."""
import base64
import copy
import hashlib
from datetime import timedelta
from sqlalchemy import select
import pytest

from test_verification_ai import context, client, submit, SECRET
from app.models import SubmissionStatus as Status, now_utc
from app.orm_models import JudgeJobRow, JudgeNodeRow, TestcaseRow as Case, VerificationExecutionRow as Execution
from app.services.store import store
from app.services import verification_execution as execution
from app.services.storage import object_storage
from app.settings import settings


def setup(c, count=4):
    with c['sessions']() as db:
        first = db.scalar(select(Case))
        for index in range(2, count+1):
            db.add(Case(testcase_set_id=first.testcase_set_id, display_order=index,
                        input_storage_key=first.input_storage_key, output_storage_key=first.output_storage_key,
                        input_sha256=first.input_sha256, output_sha256=first.output_sha256))
        db.commit()
    sid = submit(c, claim=False)
    job = next(job for job in store.claim_jobs(c['node'].judge_node_id, SECRET, 1) if job['submission']['submission_id'] == sid)
    return sid, job


def case(index, status='accepted', output=b'5\n'):
    return {'index': index, 'final_status': status, 'runtime_ms': index*10, 'memory_kb': index*1024,
            'output_base64': base64.b64encode(output).decode(), 'output_total_bytes': len(output), 'output_truncated': False}


def progress(job, cases):
    return client.post('/api/internal/judge/jobs/'+job['judge_job_id']+'/progress', json={
        'node_secret': SECRET, 'lease_token': job['lease_token'], 'status': 'judging',
        'progress_current': len(cases), 'progress_total': len(job['testcases']), 'testcase_results': cases})


def finish(job, cases, status='wrong_answer', failed=2):
    return client.post('/api/internal/judge/jobs/'+job['judge_job_id']+'/result', json={
        'node_secret': SECRET, 'lease_token': job['lease_token'], 'final_status': status,
        'failed_testcase_order': failed, 'runtime_ms': 99999, 'memory_kb': 99999, 'testcase_results': cases})


def test_full_results_progress_outputs_and_shared_history(context):
    c = context
    sid, job = setup(c)
    assert job['collect_all_results'] is True and job['submission']['submission_kind'] == 'operator_test'
    rows = [case(1), case(2, 'wrong_answer', b'4\n'), case(3, 'time_limit_exceeded', b''), case(4)]
    assert progress(job, rows[:2]).status_code == 200
    url = c['base'] + f'/verification-runs/{sid}/testcases'
    partial = client.get(url, headers=c['headers']['author']).json()['data']
    assert partial['execution']['completed'] == 2 and not partial['execution']['full_suite_complete']
    assert partial['indices'] == [1,2,3,4]
    response = finish(job, rows)
    assert response.status_code == 200, response.text
    assert response.json()['data']['submission']['progress_current'] == 4
    result = client.get(url, headers=c['headers']['owner']).json()['data']
    assert len(result['testcases']) == 4 and result['execution']['full_suite_complete']
    assert result['execution']['runtime_ms'] == 40 and result['execution']['memory_kb'] == 4096
    assert client.get(url, headers=c['headers']['author']).json()['data'] == result
    assert finish(job, rows).status_code == 200  # Lost response after a committed result.
    detail = client.get(url+'/2', headers=c['headers']['owner']).json()['data']
    assert detail['input']['text'] == '2 3\n' and detail['expected']['text'] == '5\n'
    assert detail['actual']['text'] == '4\n' and detail['runtime_ms'] == 20
    with c['sessions']() as db:
        saved = db.get(Execution, sid)
        assert all('output_base64' not in item for item in saved.results)
        assert saved.node_name == 'test-agent'
    history = client.get(c['base']+'/verification-runs', headers=c['headers']['owner']).json()['data']['runs'][0]
    assert history['execution']['full_suite_complete'] and 'testcases' not in history['execution']
    assert history['analysis'] is None and not c['calls']


def test_unsupported_nodes_cannot_claim_full_runs_and_contest_path_stays_fail_fast(context):
    c = context
    sid = submit(c, claim=False)
    with c['sessions']() as db:
        db.get(JudgeNodeRow, c['node'].judge_node_id).execution_capabilities=[]
        db.commit()
    assert store.claim_jobs(c['node'].judge_node_id, SECRET, 2) == []
    plain = store.create_operator_test_submission(c['cid'], c['pid'], 'cpp17', 'int main(){}')
    job = store.claim_jobs(c['node'].judge_node_id, SECRET, 2)[0]
    assert job['submission']['submission_id'] == plain.submission_id and not job.get('collect_all_results')
    assert finish(job, [case(1)], 'accepted', None).status_code == 409
    result = store.report_judge_result(job['judge_job_id'], SECRET, job['lease_token'], Status.WRONG_ANSWER, None, None, 1)
    assert result[0].status == Status.WRONG_ANSWER
    assert store.get_submission(sid).status == Status.WAITING


@pytest.mark.parametrize('mutation', ['incomplete', 'duplicate', 'unknown', 'invalid_base64', 'wrong_size', 'wrong_aggregate'])
def test_invalid_full_suite_results_are_rejected_without_persisting(context, mutation):
    c = context
    sid, job = setup(c, 2)
    values = [case(1), case(2,'wrong_answer')]
    status, failed = 'wrong_answer', 2
    if mutation == 'incomplete': values = values[:1]
    if mutation == 'duplicate': values[1] = copy.deepcopy(values[0])
    if mutation == 'unknown': values[1]['index'] = 99
    if mutation == 'invalid_base64': values[1]['output_base64'] = '!!!!'
    if mutation == 'wrong_size': values[1]['output_total_bytes'] = 1
    if mutation == 'wrong_aggregate': status, failed = 'accepted', None
    response = finish(job, values, status, failed)
    assert response.status_code == 409, response.text
    with c['sessions']() as db:
        assert db.get(Execution,sid).results == []
    assert store.get_submission(sid).status == Status.PREPARING


def test_changed_results_and_foreign_or_expired_leases_are_rejected(context):
    c = context
    sid, job = setup(c, 2)
    assert progress(job,[case(1)]).status_code == 200
    assert progress(job,[case(1,'wrong_answer')]).status_code == 409
    assert progress({**job,'lease_token':'foreign'},[case(2)]).status_code == 409
    with c['sessions']() as db:
        db.get(JudgeJobRow,job['judge_job_id']).leased_at=now_utc()-timedelta(seconds=settings.judge_lease_timeout_seconds+1)
        db.commit()
    assert progress(job,[case(2)]).status_code == 409
    new = store.claim_jobs(c['node'].judge_node_id,SECRET,1)[0]
    assert new['lease_token'] != job['lease_token']
    assert execution.case_list(c['cid'],c['pid'],sid)['testcases']==[]
    assert progress(job,[case(2)]).status_code == 409


@pytest.mark.parametrize('status',['compile_error','system_error'])
def test_compile_and_infrastructure_failures_are_incomplete_not_fake_full_coverage(context,status):
    c=context
    sid,job=setup(c)
    assert finish(job,[],status,None).status_code == 200
    saved=execution.case_list(c['cid'],c['pid'],sid)
    assert saved['execution']['completed']==0 and not saved['execution']['full_suite_complete']


def test_private_cases_require_resource_permission_and_the_original_problem(context):
    c=context
    sid,job=setup(c,1)
    assert finish(job,[case(1)],'accepted',None).status_code==200
    path=c['base']+f'/verification-runs/{sid}/testcases'
    for suffix in ['', '/1']:
        assert client.get(path+suffix).status_code==401
        assert client.get(path+suffix,headers=c['headers']['reviewer']).status_code==403
        assert client.get(path.replace(c['pid'],'different-problem')+suffix,headers=c['headers']['owner']).status_code==404
    assert client.get(path+'/2',headers=c['headers']['owner']).status_code==404


def test_output_truncation_is_explicit_and_empty_output_is_available(context):
    c=context
    sid,job=setup(c,2)
    truncated=case(1,'wrong_answer',b'abc');truncated.update(output_total_bytes=100,output_truncated=True)
    assert finish(job,[truncated,case(2,'wrong_answer',b'')],'wrong_answer',1).status_code==200
    first=execution.case_detail(c['cid'],c['pid'],sid,1)['actual']
    second=execution.case_detail(c['cid'],c['pid'],sid,2)['actual']
    assert first=={'text':'abc','size':100,'truncated':True}
    assert second=={'text':'','size':0,'truncated':False}


def test_matching_resource_failure_anywhere_controls_analysis_eligibility(context):
    c=context
    sid,job=setup(c,3)
    assert finish(job,[case(1),case(2,'wrong_answer'),case(3,'time_limit_exceeded')]).status_code==200
    from app.orm_models import VerificationRunRow
    with c['sessions']() as db:
        run=db.get(VerificationRunRow,sid)
        run.expected_status='time_limit_exceeded'
        db.commit()
        assert execution.expectation_matches('time_limit_exceeded','wrong_answer',db.get(Execution,sid))
    path=c['base']+f'/verification-runs/{sid}/analysis'
    result=client.post(path,headers=c['headers']['owner'])
    assert result.status_code==409 and result.json()['error']['code']=='verification_no_mismatch'
    assert not c['calls']


def test_system_error_retry_clears_previous_coverage_and_rejects_previous_lease(context):
    c=context
    sid,job=setup(c,2)
    assert progress(job,[case(1)]).status_code==200
    assert finish(job,[],'system_error',None).status_code==200
    assert finish({**job,'lease_token':'틀린 토큰'},[],'system_error',None).status_code==409
    store.retry_system_error_submission(c['cid'],sid)
    assert execution.case_list(c['cid'],c['pid'],sid)['execution']['completed']==0
    retried=store.claim_jobs(c['node'].judge_node_id,SECRET,1)[0]
    assert progress(job,[case(2)]).status_code==409
    assert finish(retried,[case(1),case(2)],'accepted',None).status_code==200


def test_actual_output_preview_keeps_its_own_truncation_flag(context, monkeypatch):
    monkeypatch.setattr(execution, "MAX_PREVIEW", 16)
    c = context
    sid, job = setup(c, count=1)
    output = b'x' * (execution.MAX_PREVIEW + 8)
    response = finish(job, [case(1, output=output)], status='accepted', failed=None)
    assert response.status_code == 200, response.text
    detail = client.get(c['base'] + f'/verification-runs/{sid}/testcases/1', headers=c['headers']['owner']).json()['data']
    assert detail['actual']['truncated'] is True
    assert len(detail['actual']['text']) == execution.MAX_PREVIEW
    assert detail['actual']['size'] == len(output)
