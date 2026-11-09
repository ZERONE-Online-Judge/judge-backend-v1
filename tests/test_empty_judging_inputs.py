"""Empty test suites fail before dispatch and cannot masquerade as lease loss."""
from types import SimpleNamespace
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from app.models import SubmissionStatus, now_utc
from app.orm_models import (JudgeJobRow, ParticipantTeamRow, ProblemRow, RejudgingRow,
    SubmissionRow, TeamMemberRow, TestcaseRow as CaseRow, TestcaseSetRow as CaseSetRow,
    VerificationExecutionRow, VerificationRunRow)
from app.services import participant_preview, rejudging
from app.services.errors import AppError
from app.services.judging_inputs import QUEUED_TESTCASES_NOT_READY
from app.services.store import store
from test_verification_ai import context, client, SECRET, submit
from test_verification_execution import case, finish, progress, setup


def remove_active_cases(c, mode="empty"):
    with c['sessions']() as db:
        active = db.scalar(select(CaseSetRow).where(CaseSetRow.problem_id == c['pid']))
        if mode == "inactive":
            active.is_active = False
        else:
            db.execute(delete(CaseRow).where(CaseRow.testcase_set_id == active.testcase_set_id))
            if mode == "missing":
                db.delete(active)
        db.commit()


def counts(c):
    with c['sessions']() as db:
        return [db.scalar(select(func.count()).select_from(model)) for model in (
            SubmissionRow, JudgeJobRow, VerificationRunRow, VerificationExecutionRow, RejudgingRow)]


@pytest.mark.parametrize("mode", ["missing", "inactive", "empty"])
@pytest.mark.parametrize("kind", ["participant", "mock", "operator", "registered"])
def test_creation_requires_real_active_cases_and_leaves_no_records(context, mode, kind):
    c = context
    remove_active_cases(c, mode)
    if kind == 'participant':
        with c['sessions']() as db:
            did=db.get(ProblemRow,c['pid']).division_id
            db.add(ParticipantTeamRow(participant_team_id='team',contest_id=c['cid'],division_id=did,team_name='Team'))
            db.flush()
            db.add(TeamMemberRow(team_member_id='member',contest_id=c['cid'],participant_team_id='team',
                role='leader',name='Member',email='member@example.com'))
            db.commit()
    before = counts(c)
    if kind in {"operator", "registered"}:
        body = {'language': 'python311', 'source_code': c['source']}
        if kind == 'registered':
            body['verification_asset_id'] = c['aid']
        response = client.post(c['base'] + '/test-submissions', headers=c['headers']['owner'], json=body)
        assert response.status_code == 422, response.text
        error = response.json()['error']
        assert error['code'] == 'testcases_not_ready' and error['request_id']
        assert '활성 테스트' in error['message'] and 'lease' not in error['message']
    else:
        with pytest.raises(AppError) as rejected:
            if kind == 'participant':
                store.create_submission(c['cid'], c['pid'], 'member@example.com', 'python311', c['source'])
            else:
                store.create_mock_submission(c['cid'], c['pid'], 'python311', c['source'])
        assert rejected.value.code == 'testcases_not_ready'
    assert counts(c) == before


def test_participant_preview_is_test_based_but_does_not_create_partial_records(context, monkeypatch):
    c = context
    monkeypatch.setattr(participant_preview, 'SessionLocal', c['sessions'])
    remove_active_cases(c)
    before = counts(c)
    with c['sessions']() as db:
        division_id = db.get(ProblemRow, c['pid']).division_id
    with pytest.raises(AppError) as rejected:
        participant_preview.create_preview_submission(c['cid'], c['pid'],
            {'division': SimpleNamespace(division_id=division_id)}, 'python311', c['source'])
    assert rejected.value.code == 'testcases_not_ready'
    assert counts(c) == before


def test_nonempty_active_inputs_still_allow_each_kind(context):
    c=context
    with c['sessions']() as db:
        did=db.get(ProblemRow,c['pid']).division_id
        db.add(ParticipantTeamRow(participant_team_id='team',contest_id=c['cid'],division_id=did,team_name='Team'))
        db.flush()
        db.add(TeamMemberRow(team_member_id='member',contest_id=c['cid'],participant_team_id='team',
            role='leader',name='Member',email='member@example.com'))
        db.commit()
    regular=store.create_submission(c['cid'],c['pid'],'member@example.com','python311',c['source'])
    mock=store.create_mock_submission(c['cid'],c['pid'],'python311',c['source'])
    operator=store.create_operator_test_submission(c['cid'],c['pid'],'python311',c['source'])
    registered=submit(c,claim=False)
    assert all(store.get_submission(sid).status=='waiting' for sid in (
        regular.submission_id,mock.submission_id,operator.submission_id,registered))


def test_other_division_empty_problem_remains_hidden_before_readiness_check(context):
    c=context
    with c['sessions']() as db:
        did=db.get(ProblemRow,c['pid']).division_id
    store.create_participant_team(c['cid'],did,'Participant team','Participant','member@example.com',[])
    token=store.verify_otp(c['cid'],'member@example.com','',force_new_session=True)[-1]
    other_division=store.create_contest_division(c['cid'],'other','Other')
    other_problem=store.create_problem(c['cid'],other_division.division_id,'A','Private test setup','',1000,128,{},1)
    store.update_contest_settings(c['cid'],status='running',start_at=now_utc()-timedelta(hours=1),
        end_at=now_utc()+timedelta(hours=1))
    before=counts(c)
    response=client.post(f"/api/contests/{c['cid']}/problems/{other_problem.problem_id}/submissions",
        headers={'Authorization':'Bearer '+token},json={'language':'python311','source_code':c['source']})
    assert response.status_code==404
    assert response.json()['error']['code']=='not_found'
    assert counts(c)==before


def test_failed_retry_preserves_original_result_and_queue(context):
    c = context
    sid = submit(c, expected_status=SubmissionStatus.SYSTEM_ERROR)
    before = store.get_submission(sid).model_dump(mode='json')
    with c['sessions']() as db:
        job = db.scalar(select(JudgeJobRow).where(JudgeJobRow.submission_id == sid))
        original = (job.status, job.queue_position, job.leased_at, job.assigned_node_id)
    remove_active_cases(c)
    with pytest.raises(AppError) as rejected:
        store.retry_system_error_submission(c['cid'], sid)
    assert rejected.value.code == 'testcases_not_ready'
    assert store.get_submission(sid).model_dump(mode='json') == before
    with c['sessions']() as db:
        job = db.scalar(select(JudgeJobRow).where(JudgeJobRow.submission_id == sid))
        assert (job.status, job.queue_position, job.leased_at, job.assigned_node_id) == original


@pytest.mark.parametrize('mode', ['missing', 'inactive', 'empty'])
def test_claim_finishes_old_empty_jobs_and_claims_later_ready_job_in_same_request(context, mode):
    c = context
    sid = submit(c, claim=False)
    with c['sessions']() as db:
        good_problem = ProblemRow(contest_id=c['cid'], division_id=db.get(ProblemRow,c['pid']).division_id,
            problem_code='Z', title='Ready', statement='', time_limit_ms=1000, memory_limit_mb=128, display_order=26)
        db.add(good_problem); db.flush()
        active = CaseSetRow(problem_id=good_problem.problem_id, version=1, is_active=True)
        db.add(active);db.flush()
        first=db.scalar(select(CaseRow))
        db.add(CaseRow(testcase_set_id=active.testcase_set_id,display_order=1,
            input_storage_key=first.input_storage_key,output_storage_key=first.output_storage_key,
            input_sha256=first.input_sha256,output_sha256=first.output_sha256))
        db.commit()
        good_pid=good_problem.problem_id
    good = store.create_operator_test_submission(c['cid'],good_pid,'python311',c['source'])
    remove_active_cases(c,mode)
    jobs = store.claim_jobs(c['node'].judge_node_id, SECRET, 1)
    assert [job['submission']['submission_id'] for job in jobs] == [good.submission_id]
    failed = store.get_submission(sid)
    assert failed.status == 'system_error' and failed.judge_message == QUEUED_TESTCASES_NOT_READY
    with c['sessions']() as db:
        job = db.scalar(select(JudgeJobRow).where(JudgeJobRow.submission_id == sid))
        assert job.status == 'failed' and job.lease_token is None and job.assigned_node_id is None
        execution = db.get(VerificationExecutionRow, sid)
        assert execution.results == [] and not execution.full_suite_complete
    assert store.claim_jobs(c['node'].judge_node_id, SECRET, 1) == []


def test_typed_payload_error_keeps_real_lease_and_allows_human_readable_failure(context):
    c = context
    sid, job = setup(c, count=2)
    rows = [case(1), case(2)]
    assert progress(job, rows).status_code == 200
    response = finish(job, rows, 'wrong_answer', 2)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'testcase_results_invalid'
    assert response.json()['error']['details']['reason'] == 'aggregate_mismatch'
    # Only an infrastructure failure can finalize already complete evidence.
    # A compile-error claim after all tests ran remains contradictory.
    contradictory = finish(job, [], 'compile_error', None)
    assert contradictory.status_code == 422
    assert contradictory.json()['error']['details']['reason'] == 'aggregate_mismatch'
    with c['sessions']() as db:
        saved = db.get(JudgeJobRow, job['judge_job_id'])
        assert saved.status == 'running' and saved.lease_token == job['lease_token']
    # Real ownership errors still use 409 and never change the verdict.
    assert finish({**job,'lease_token':'old-token'}, [], 'system_error', None).status_code == 409
    response = client.post('/api/internal/judge/jobs/'+job['judge_job_id']+'/result',json={
        'node_secret':SECRET,'lease_token':job['lease_token'],'final_status':'system_error',
        'judge_message':'채점 결과 증거를 확인하지 못했습니다. 관리자에게 문의해 주세요.'})
    assert response.status_code == 200, response.text
    assert store.get_submission(sid).status == 'system_error'
    with c['sessions']() as db:
        saved = db.get(VerificationExecutionRow,sid)
        assert len(saved.results) == 2 and not saved.full_suite_complete


def test_empty_staged_rejudging_request_is_atomic(context):
    c = context
    sub = store.create_operator_test_submission(c['cid'],c['pid'],'python311',c['source'])
    with c['sessions']() as db:
        db.get(SubmissionRow,sub.submission_id).status='wrong_answer'
        job = db.scalar(select(JudgeJobRow).where(JudgeJobRow.submission_id==sub.submission_id))
        job.status='succeeded';db.commit()
    remove_active_cases(c)
    before = counts(c)
    with pytest.raises(AppError) as rejected:
        rejudging.create(store,c['cid'],submission_id=sub.submission_id,reason='Fix tests',
            actor=SimpleNamespace(display_name='Operator',email='operator@example.com'))
    assert rejected.value.code == 'testcases_not_ready'
    assert counts(c) == before


def test_old_empty_staged_manifest_finishes_candidate_and_preserves_official_result(context):
    c=context
    sub=store.create_operator_test_submission(c['cid'],c['pid'],'python311',c['source'])
    job=store.claim_jobs(c['node'].judge_node_id,SECRET,1)[0]
    store.report_judge_result(job['judge_job_id'],SECRET,job['lease_token'],
        SubmissionStatus.WRONG_ANSWER,None,'Original result',1)
    original=store.get_submission(sub.submission_id).model_dump(mode='json')
    batch=rejudging.create(store,c['cid'],submission_id=sub.submission_id,reason='Fix tests',
        actor=SimpleNamespace(display_name='Operator',email='operator@example.com'))
    with c['sessions']() as db:
        saved=db.get(RejudgingRow,batch['rejudging_id'])
        saved.judge_context={**saved.judge_context,'testcases':[]}
        db.commit()
    assert store.claim_jobs(c['node'].judge_node_id,SECRET,1)==[]
    assert store.get_submission(sub.submission_id).model_dump(mode='json')==original
    result=rejudging.detail(store,c['cid'],batch['rejudging_id'])
    assert result['status']=='blocked'
    assert result['items'][0]['candidate_result']['status']=='system_error'
    assert result['items'][0]['candidate_result']['judge_message']==QUEUED_TESTCASES_NOT_READY
