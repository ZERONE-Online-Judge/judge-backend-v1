from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from fastapi.encoders import jsonable_encoder

from app.models import StaffAccount, SubmissionStatus, now_utc
from app.orm_models import (JudgeJobRow, ProblemRow, RejudgingItemRow, RejudgingRow,
    SubmissionRow, TestcaseRow as CaseRow, TestcaseSetRow as CaseSetRow, VerificationRunRow, VerificationTrialRow)
from app.services import rejudging
from app.services.errors import AppError
from app.services.store import store
from app.tools import judge_nodes
from tests.test_judge_security import database, leased, client, SECRET

ACTOR = StaffAccount(staff_account_id='staff', email='operator@example.com', display_name='Operator',
    contest_scopes={'contest': ['contest.submission.view', 'contest.problem.test']})


@pytest.fixture
def finished(database, leased):
    store.report_judge_result('job', SECRET, 'original-lease', SubmissionStatus.WRONG_ANSWER,
        'compile', 'old result', 1, 123, 456)
    with database() as db:
        db.add(ProblemRow(problem_id='problem', contest_id='contest', division_id='division',
            problem_code='A', title='A', statement='sum', time_limit_ms=1000, memory_limit_mb=128, display_order=1))
        db.add(CaseSetRow(testcase_set_id='set', problem_id='problem', version=1, is_active=True))
        db.add(CaseRow(testcase_id='case', testcase_set_id='set', display_order=1,
            input_storage_key='input', output_storage_key='output', input_sha256='a'*64, output_sha256='b'*64))
        db.commit()
    return leased


def create(**scope):
    return rejudging.create(store, 'contest', reason='Fix testcase', actor=ACTOR, **(scope or {'submission_id': 'submission'}))


def claim(node):
    jobs = store.claim_jobs(node.judge_node_id, SECRET, 5)
    assert jobs
    return jobs


def report(job, status=SubmissionStatus.ACCEPTED):
    return store.report_judge_result(job['judge_job_id'], SECRET, job['lease_token'], status,
        None, 'candidate result', None if status == SubmissionStatus.ACCEPTED else 1, 50, 100)


def decide(batch, action):
    return rejudging.decide(store, 'contest', batch['rejudging_id'], action, ACTOR)


def test_staging_claim_progress_result_do_not_change_official_submission(database, finished):
    before = store.get_submission('submission').model_dump(mode='json')
    batch = create()
    assert batch['status'] == 'pending' and batch['total'] == 1
    with pytest.raises(AppError):
        create()
    job = claim(finished)[0]
    assert job['problem']['time_limit_ms'] == 1000
    assert job['testcases'][0]['input_sha256'] == 'a'*64
    store.update_judge_progress(job['judge_job_id'], SECRET, job['lease_token'], SubmissionStatus.JUDGING, 1, 1)
    assert store.get_submission('submission').model_dump(mode='json') == before
    assert report(job)[0].status == 'accepted'
    assert store.get_submission('submission').model_dump(mode='json') == before
    result = rejudging.detail(store, 'contest', batch['rejudging_id'])
    assert result['status'] == 'ready' and result['changed_verdicts'] == 1
    assert result['items'][0]['original_result']['judge_message'] == 'old result'
    assert result['items'][0]['candidate_result']['runtime_ms'] == 50
    assert store.judge_jobs_by_submission_ids(['submission'])['submission'].judge_job_id == 'job'
    assert store.pending_queue_ranks('contest') == {}
    decide(batch, 'apply')
    updated = store.get_submission('submission').model_dump(mode='json')
    assert updated['status'] == 'accepted'
    for field in ('submission_id', 'submitted_at', 'language', 'source_code'):
        assert updated[field] == before[field]
    assert updated['runtime_ms'] == 50
    assert rejudging.detail(store, 'contest', batch['rejudging_id'])['items'][0]['original_result']['status'] == 'wrong_answer'
    with pytest.raises(AppError):
        decide(batch, 'apply')
    assert create()['status'] == 'pending'


@pytest.mark.parametrize('running', [False, True])
def test_cancel_preserves_verdict_and_revokes_pending_or_running_lease(database, finished, running):
    batch = create()
    job = claim(finished)[0] if running else None
    assert decide(batch, 'cancel')['status'] == 'cancelled'
    if job:
        with pytest.raises(ValueError, match='lease mismatch'):
            report(job)
    assert store.get_submission('submission').status == 'wrong_answer'
    assert store.claim_jobs(finished.judge_node_id, SECRET, 5) == []
    with pytest.raises(AppError):
        decide(batch, 'apply')
    assert create()['total'] == 1


@pytest.mark.parametrize('change', ['time', 'memory', 'case', 'output', 'language'])
def test_changed_judging_materials_cannot_be_applied_and_claim_uses_snapshot(database, finished, change):
    batch = create()
    with database() as db:
        if change == 'time': db.get(ProblemRow, 'problem').time_limit_ms = 3000
        elif change == 'memory': db.get(ProblemRow, 'problem').memory_limit_mb = 256
        elif change == 'language': db.get(ProblemRow, 'problem').language_resource_limits = {'python311': {'time_limit_ms': 9999}}
        elif change == 'case': db.get(CaseRow, 'case').time_limit_ms_override = 100
        else: db.get(CaseRow, 'case').output_sha256 = 'c'*64
        db.commit()
    job = claim(finished)[0]
    assert job['problem']['time_limit_ms'] == 1000 and job['problem']['memory_limit_mb'] == 128
    assert job['testcases'][0]['output_sha256'] == 'b'*64
    report(job)
    with pytest.raises(AppError, match='변경'):
        decide(batch, 'apply')
    assert rejudging.detail(store, 'contest', batch['rejudging_id'])['status'] == 'ready'
    assert store.get_submission('submission').status == 'wrong_answer'
    decide(batch, 'cancel')


@pytest.mark.parametrize('change', ['status', 'source', 'language'])
def test_official_changes_do_not_get_overwritten(database, finished, change):
    batch = create()
    report(claim(finished)[0])
    with database() as db:
        submission = db.get(SubmissionRow, 'submission')
        if change == 'status': submission.status = 'runtime_error'
        elif change == 'source': submission.source_code = 'print(43)'
        else: submission.language = 'cpp17'
        db.commit()
    with pytest.raises(AppError, match='기존 제출'):
        decide(batch, 'apply')
    assert rejudging.detail(store, 'contest', batch['rejudging_id'])['status'] == 'ready'


def test_incomplete_or_system_error_never_replaces_official_result(database, finished):
    batch = create()
    with pytest.raises(AppError): decide(batch, 'apply')
    report(claim(finished)[0], SubmissionStatus.SYSTEM_ERROR)
    assert rejudging.detail(store, 'contest', batch['rejudging_id'])['status'] == 'blocked'
    with pytest.raises(AppError): decide(batch, 'apply')
    assert store.get_submission('submission').status == 'wrong_answer'


def test_bulk_scope_skips_pending_and_excludes_operator_trials_and_other_problems(database, finished):
    with database() as db:
        for sid, kind, status, problem in (
            ('second', 'participant', 'accepted', 'problem'),
            ('waiting', 'participant', 'waiting', 'problem'),
            ('preview', 'participant_preview', 'accepted', 'problem'),
            ('operator', 'operator_test', 'accepted', 'problem'),
            ('other', 'participant', 'accepted', 'another'),
        ):
            db.add(SubmissionRow(submission_id=sid, contest_id='contest', division_id='division',
                problem_id=problem, language='python311', source_code='print(1)', status=status, submission_kind=kind))
        db.commit()
    batch = create(problem_id='problem')
    assert batch['total'] == 2 and batch['scope']['skipped_pending'] == 1
    results = rejudging.detail(store, 'contest', batch['rejudging_id'], limit=1)
    assert results['has_next'] and len(results['items']) == 1
    jobs = claim(finished)
    assert len(jobs) == 2
    for job in jobs: report(job)
    decide(batch, 'apply')
    assert store.get_submission('second').status == 'accepted'
    assert store.get_submission('waiting').status == 'waiting'
    assert rejudging.list_batches(store, 'contest')['total'] == 1


@pytest.mark.parametrize('revoke', [False, True])
def test_lease_recovery_or_node_revocation_only_resets_candidate(database, finished, revoke):
    batch = create()
    job = claim(finished)[0]
    if revoke:
        judge_nodes.update_credential(finished.node_name, enabled=False)
    else:
        with database() as db:
            db.get(JudgeJobRow, job['judge_job_id']).leased_at = now_utc() - timedelta(days=1)
            db.commit()
        store.update_node_heartbeat(finished.judge_node_id, SECRET, 2, 2, 0)
    assert store.get_submission('submission').status == 'wrong_answer'
    assert rejudging.detail(store, 'contest', batch['rejudging_id'])['items'][0]['candidate_result']['status'] == 'waiting'
    with pytest.raises(ValueError): report(job)


def test_parallel_duplicate_creation_is_rejected(database, finished):
    def request(_):
        try: return create()['status']
        except AppError as error: return str(error.status_code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(request, range(2))) == ['409', 'pending']
    with database() as db:
        assert db.scalar(select(func.count()).select_from(RejudgingRow)) == 1
        assert db.scalar(select(func.count()).select_from(RejudgingItemRow)) == 1


def test_api_authorization_validation_scope_and_decision(database, finished, monkeypatch):
    account = ACTOR.model_copy(deep=True)
    monkeypatch.setattr(store, 'get_staff_by_access_token', lambda token: account if token == 'staff' else None)
    path = '/api/operator/contests/contest/rejudgings'
    headers = {'Authorization': 'Bearer staff'}
    body = {'submission_id': 'submission', 'reason': 'test'}
    assert client.post(path, json=body).status_code == 401
    account.contest_scopes['contest'] = ['contest.submission.view']
    assert client.post(path, json=body, headers=headers).status_code == 403
    account.contest_scopes['contest'].append('contest.problem.test')
    for bad in ({'reason': 'test'}, {'reason': '  ', 'problem_id': 'problem'},
        {'reason': 'test', 'submission_id': 'submission', 'problem_id': 'problem'},
        {'reason': 'test', 'submission_id': 'submission', 'team_id': 'team'}):
        assert client.post(path, json=bad, headers=headers).status_code == 422
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    batch = response.json()['data']
    detail_path = path + '/' + batch['rejudging_id']
    assert client.get(detail_path, headers=headers).status_code == 200
    assert client.get(detail_path.replace('/contest/', '/another/'), headers=headers).status_code == 403
    assert client.get(path, headers=headers).json()['data']['total'] == 1
    account.contest_scopes['contest'] = ['contest.submission.view']
    assert client.post(detail_path + '/apply', headers=headers).status_code == 403
    assert client.post(detail_path + '/cancel', headers=headers).status_code == 403
    account.contest_scopes['contest'].append('contest.problem.test')
    report(claim(finished)[0])
    assert client.post(detail_path + '/apply', headers=headers).status_code == 200
    assert client.post(detail_path + '/cancel', headers=headers).status_code == 409


def test_score_changes_only_on_apply_and_use_original_time(database, finished):
    from app.orm_models import ContestRow, ContestDivisionRow, ParticipantTeamRow, ScoreboardReleaseRow
    start = now_utc() - timedelta(hours=1)
    with database() as db:
        db.add(ContestRow(contest_id='contest', title='Contest', organization_name='Test', overview='',
            status='ended', start_at=start, end_at=start + timedelta(minutes=30), freeze_at=start + timedelta(minutes=20)))
        db.add(ContestDivisionRow(division_id='division', contest_id='contest', code='all', name='All'))
        db.add(ParticipantTeamRow(participant_team_id='team', contest_id='contest', division_id='division', team_name='Team'))
        row = db.get(SubmissionRow, 'submission')
        row.participant_team_id = 'team'
        row.submitted_at = start + timedelta(minutes=10)
        db.commit()
    before = store.scoreboard_rows('contest', 'division')['rows'][0]
    assert before['solved'] == 0 and before['submission_count'] == 1
    batch = create()
    report(claim(finished)[0])
    assert store.scoreboard_rows('contest', 'division')['rows'][0] == before
    decide(batch, 'apply')
    after = store.scoreboard_rows('contest', 'division')['rows'][0]
    assert after['solved'] == 1 and after['penalty'] == 10 and after['submission_count'] == 1
    # An already released scoreboard remains a fixed historical snapshot.
    with database() as db:
        db.add(ScoreboardReleaseRow(contest_id='contest', division_id='division', mode='all', strategy='manual',
            snapshot_rows=[jsonable_encoder(after)], revealed_ranks=[]))
        db.commit()
    public_before = store.scoreboard_rows('contest', 'division', public_view=True)
    second = create()
    report(claim(finished)[0], SubmissionStatus.WRONG_ANSWER)
    decide(second, 'apply')
    assert store.scoreboard_rows('contest', 'division')['rows'][0]['solved'] == 0
    assert store.scoreboard_rows('contest', 'division', public_view=True) == public_before


def test_apply_refreshes_verification_context_without_erasing_previous_analysis(database, finished):
    with database() as db:
        row = db.get(SubmissionRow, 'submission')
        row.submission_kind = 'operator_test'
        db.add(VerificationRunRow(submission_id='submission', contest_id='contest', problem_id='problem',
            asset_id='asset', expected_status='accepted', context_hash='old-context', analysis_id='old-analysis'))
        db.commit()
    batch = create()
    report(claim(finished)[0])
    with database() as db:
        assert db.get(VerificationRunRow, 'submission').analysis_id == 'old-analysis'
    decide(batch, 'apply')
    with database() as db:
        run = db.get(VerificationRunRow, 'submission')
        assert run.context_hash and run.context_hash != 'old-context'
        assert run.analysis_id is None
        item = db.scalar(select(RejudgingItemRow))
        assert item.original_result['analysis_id'] == 'old-analysis'
        assert item.original_result['verification_context_hash'] == 'old-context'


def test_bulk_filters_language_and_team_and_new_submissions_are_not_added(database, finished):
    with database() as db:
        db.get(SubmissionRow, 'submission').participant_team_id = 'team'
        db.add(SubmissionRow(submission_id='cpp', contest_id='contest', division_id='division',
            problem_id='problem', participant_team_id='team', language='cpp17', source_code='int main(){}', status='accepted'))
        db.commit()
    batch = create(problem_id='problem', team_id='team', language='python311')
    assert batch['total'] == 1
    with database() as db:
        db.add(SubmissionRow(submission_id='later', contest_id='contest', division_id='division',
            problem_id='problem', participant_team_id='team', language='python311', source_code='print(0)', status='accepted'))
        db.commit()
    assert rejudging.detail(store, 'contest', batch['rejudging_id'])['total'] == 1
    with pytest.raises(AppError):
        rejudging.detail(store, 'another', batch['rejudging_id'])


def test_parallel_apply_or_cancel_has_only_one_winner(database, finished):
    batch = create()
    report(claim(finished)[0])
    def request(action):
        try: return decide(batch, action)['status']
        except AppError as error: return str(error.status_code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ['apply', 'cancel']))
    assert results.count('409') == 1
    stored = rejudging.detail(store, 'contest', batch['rejudging_id'])
    assert stored['status'] in {'applied', 'cancelled'}
    assert store.get_submission('submission').status == ('accepted' if stored['status'] == 'applied' else 'wrong_answer')


def test_normal_submissions_take_priority_over_rejudging(database, finished):
    create()
    with database() as db:
        db.add(SubmissionRow(submission_id='normal', contest_id='contest', division_id='division', problem_id='problem',
            language='python311', source_code='print(0)', status='waiting'))
        db.add(JudgeJobRow(judge_job_id='normal-job', submission_id='normal', contest_id='contest', division_id='division',
            status='pending', queue_position=999))
        db.commit()
    first = store.claim_jobs(finished.judge_node_id, SECRET, 1)[0]
    assert first['judge_job_id'] == 'normal-job'


def test_cached_bundle_reuse_and_per_case_limits_are_versioned(database, finished, monkeypatch):
    from app.services.storage import object_storage
    from app.orm_models import BundleWarmQueueItemRow
    batch = create()
    with database() as db:
        manifest = db.get(RejudgingRow, batch['rejudging_id']).judge_context
        key = manifest['bundle_storage_key']
        assert db.scalar(select(BundleWarmQueueItemRow)).status == 'pending'
        case = db.get(CaseRow, 'case')
        problem = db.get(ProblemRow, 'problem')
        case.time_limit_ms_override = 800
        assert store._judge_bundle_key('contest', problem, 'set', [case], []) != key
        case.time_limit_ms_override = None
        case.memory_limit_mb_override = 64
        assert store._judge_bundle_key('contest', problem, 'set', [case], []) != key
    monkeypatch.setattr(object_storage, 'size_bytes', lambda storage_key: 123 if storage_key == key else None)
    monkeypatch.setattr(object_storage, 'internal_presigned_get_url', lambda storage_key: 'https://storage.local/' + storage_key)
    job = claim(finished)[0]
    assert job['bundle_url'] == 'https://storage.local/' + key
    assert 'bundle_storage_key' not in job


def test_active_rejudging_prevents_problem_deletion(database, finished):
    batch = create()
    with pytest.raises(AppError):
        store.delete_problem('contest', 'problem')
    decide(batch, 'cancel')
    assert store.delete_problem('contest', 'problem').problem_id == 'problem'
    with database() as db:
        assert db.scalar(select(RejudgingItemRow)) is None
        assert db.scalar(select(RejudgingRow)) is None
        assert db.get(SubmissionRow, 'submission') is None
