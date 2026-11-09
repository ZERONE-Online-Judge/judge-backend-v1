from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, func

from app.models import SubmissionStatus
from app.orm_models import JudgeJobRow, SubmissionRow, ProblemRow, TestcaseRow as CaseRow, TestcaseSetRow as CaseSetRow
from app.services.errors import AppError
from app.services.store import store
from tests.test_judge_security import database, leased, client, SECRET


@pytest.fixture(autouse=True)
def ready_problem(database):
    # A retry is test-based, so the fixture needs actual active judging input.
    with database() as db:
        db.add(ProblemRow(problem_id='problem', contest_id='contest', division_id='division',
            problem_code='A', title='A', statement='', time_limit_ms=1000, memory_limit_mb=128, display_order=1))
        db.add(CaseSetRow(testcase_set_id='set',problem_id='problem',version=1,is_active=True))
        db.add(CaseRow(testcase_id='case',testcase_set_id='set',display_order=1,
            input_storage_key='input',output_storage_key='output',input_sha256='a'*64,output_sha256='b'*64))
        db.commit()


def fail_job():
    store.report_judge_result('job', SECRET, 'original-lease', SubmissionStatus.SYSTEM_ERROR,
        'old compile', 'old system failure', 2, 123, 456)


def test_retry_preserves_identity_clears_old_result_and_rejects_stale_lease(database, leased):
    fail_job()
    old = store.get_submission('submission')
    waiting, previous = store.retry_system_error_submission('contest', 'submission')
    assert waiting.submitted_at == old.submitted_at
    assert waiting.source_code == old.source_code
    assert waiting.submission_id == old.submission_id
    assert waiting.status == 'waiting'
    assert previous['status'] == 'system_error'
    for field in ('runtime_ms', 'memory_kb', 'compile_message', 'judge_message', 'failed_testcase_order', 'progress_current', 'progress_total'):
        assert getattr(waiting, field) is None
    with pytest.raises(AppError):
        store.retry_system_error_submission('contest', 'submission')
    with database() as db:
        assert db.scalar(select(func.count()).select_from(JudgeJobRow)) == 1
        job = db.get(JudgeJobRow, 'job')
        assert job.status == 'pending' and job.lease_token is None and job.assigned_node_id is None
        # Simulate the normal claim's new lease, then send an old result.
        job.status = 'running'; job.assigned_node_id = leased.judge_node_id
        job.lease_token = 'new-lease'
        from app.models import now_utc
        job.leased_at = now_utc(); db.commit()
    with pytest.raises(ValueError):
        store.report_judge_result('job', SECRET, 'original-lease', SubmissionStatus.ACCEPTED, None, None, None)
    finished, _ = store.report_judge_result('job', SECRET, 'new-lease', SubmissionStatus.WRONG_ANSWER, None, 'new result', 1, 20, 100)
    assert finished.status == 'wrong_answer' and finished.judge_message == 'new result'
    with pytest.raises(AppError):
        store.retry_system_error_submission('contest', 'submission')


def test_two_simultaneous_retry_requests_enqueue_once(database, leased):
    fail_job()
    def retry(_):
        try:
            store.retry_system_error_submission('contest', 'submission')
            return 'queued'
        except AppError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(retry, range(2)))) == ['409', 'queued']


def test_retry_requires_submission_view_and_judging_permission(database, leased, monkeypatch):
    from app.models import StaffAccount
    account = StaffAccount(staff_account_id='staff', email='operator@example.com', display_name='Operator',
        contest_scopes={'contest': ['contest.submission.view']}, is_service_master=False)
    monkeypatch.setattr(store, 'get_staff_by_access_token', lambda token: account if token == 'staff' else None)
    fail_job()
    path='/api/operator/contests/contest/submissions/submission/rejudge'
    assert client.post(path).status_code == 401
    headers={'Authorization':'Bearer staff'}
    assert client.post(path, headers=headers).status_code == 403
    account.contest_scopes['contest'].append('contest.problem.test')
    assert client.post(path.replace('/contest/', '/another/'), headers=headers).status_code == 403
    assert client.post(path, headers=headers).status_code == 200
    assert client.post(path, headers=headers).status_code == 409
