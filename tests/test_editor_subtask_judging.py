import pytest
from sqlalchemy import select

from app.orm_models import EditorJudgeRunRow, JudgeNodeRow
from app.services import editor_judging as judging
from test_editor_judging import database, snapshot, submit, node, claim, report, case_result, EMAIL


def enable_subtasks(sessions, identity):
    with sessions() as db:
        row = db.get(JudgeNodeRow, identity)
        row.execution_capabilities = [judging.SUBTASKS, judging.FULL_VERIFICATION, 'judge-zoj-runtime-v1']
        db.commit()


def request(**changes):
    return snapshot(change=lambda manifest, files:
                    manifest.update({'scoring_mode': 'ioi', 'collect_all_results': True, **changes}))


def test_editor_points_requires_available_new_agent_and_all_case_evidence(database):
    response = submit(request())
    assert response.status_code == 503 and response.json()['error']['code'] == 'editor_subtask_unavailable'
    identity = node()
    enable_subtasks(database, identity)
    response = submit(snapshot(change=lambda m, f: m.update(scoring_mode='ioi')))
    assert response.status_code == 422 and response.json()['error']['code'] == 'editor_subtask_scope'
    assert judging.capabilities()['subtask_protocol'] == 'zoj-judge-subtasks-v1'
    assert judging.capabilities()['subtask_online_nodes'] == 1


def test_editor_points_snapshot_cannot_be_claimed_by_downgraded_agent(database):
    identity = node()
    enable_subtasks(database, identity)
    created = submit(request())
    assert created.status_code == 202, created.text
    run_id = created.json()['data']['id']
    with database() as db:
        row = db.get(JudgeNodeRow, identity)
        row.execution_capabilities = [judging.FULL_VERIFICATION, 'judge-zoj-runtime-v1']
        db.commit()
    assert claim(identity) == []
    enable_subtasks(database, identity)
    job = claim(identity)[0]
    assert job['scoring_mode'] == 'ioi' and job['collect_all_results']
    assert job['submission']['submission_kind'] == 'editor_verification'
    with database() as db:
        assert db.get(EditorJudgeRunRow, run_id).payload['scoring_mode'] == 'ioi'
    results = [case_result(2, 'wrong_answer'), case_result(7, 'accepted')]
    response = report(run_id, job['lease_token'], final_status='wrong_answer',
                      failed_testcase_order=2, testcase_results=results)
    assert response.status_code == 200, response.text
    saved = judging.get_run(EMAIL, run_id)
    assert saved['result']['full_suite_complete']
    assert [case['final_status'] for case in saved['result']['testcase_results']] == ['wrong_answer', 'accepted']


@pytest.mark.parametrize('value', ['points', None, 1])
def test_editor_snapshot_rejects_unknown_scoring_mode(database, value):
    assert submit(request(scoring_mode=value)).status_code == 422


@pytest.mark.parametrize('mode,status,failed', [('ioi', 'system_error', 7), ('icpc', 'wrong_answer', 2)])
def test_ioi_error_after_a_wrong_answer_is_recorded_as_a_judge_error(database, mode, status, failed):
    identity = node()
    enable_subtasks(database, identity)
    created = submit(request(scoring_mode=mode))
    assert created.status_code == 202, created.text
    run_id = created.json()['data']['id']
    job = claim(identity)[0]
    results = [case_result(2, 'wrong_answer'), case_result(7, 'system_error')]
    response = report(run_id, job['lease_token'], final_status=status,
                      failed_testcase_order=failed, testcase_results=results)
    assert response.status_code == 200, response.text
    saved = judging.get_run(EMAIL, run_id)
    assert saved['status'] == status and saved['result']['full_suite_complete']
    assert saved['result']['failed_testcase_order'] == failed
