"""Staff titles stay contest-scoped in answer and submission API payloads."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, now_utc
from app.services.store import store
from app.services.contest_roles import title_for_roles, title_for_scopes

client = TestClient(app)


def test_titles_cover_legacy_partial_permissions_and_operator_preview():
    assert title_for_scopes(["contest.*", "contest.problem.manage"]) == "마스터"
    assert title_for_scopes(["contest.testcase.manage", "contest.notice.view"]) == "출제진"
    assert title_for_scopes(["contest.notice.create", "contest.problem.review"]) == "검수진"
    assert title_for_scopes(["contest.view", "contest.problem.test"]) == "검수진"
    assert title_for_roles(["problems_viewer"]) == "운영진"
    assert title_for_scopes(["contest.view", "contest.problem.view"]) == "운영진"
    assert title_for_roles(["participant_preview"]) == "운영진"
    assert title_for_scopes(["contest.participant.preview"]) == "운영진"
    assert title_for_scopes([]) is None


def headers(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


@pytest.fixture(scope="module")
def workspace():
    contest = store.create_contest("Staff titles", "Test", "Test", now_utc() - timedelta(minutes=5), status=ContestStatus.RUNNING)
    cid = contest.contest_id
    division = store.create_contest_division(cid, "general", "General")
    problem = store.create_problem(cid, division.division_id, "A", "Test", "Statement", 1000, 128, {}, 1)
    master_email = f"titles-master-{uuid4().hex}@zoj.com"
    store.upsert_contest_operator(cid, master_email, "관리자", ["master"])
    member_email = f"titles-participant-{uuid4().hex}@zoj.com"
    team = store.create_participant_team(cid, division.division_id, "참가팀", "참가자", member_email, [])
    participant = {"team": team, "member": team.members[0], "division": division}
    question = store.create_question(cid, participant, "질문", "본문", "public")
    return {"cid": cid, "problem": problem, "question": question, "master": headers(master_email), "participant": participant, "participant_headers": headers(member_email)}


@pytest.mark.parametrize(("roles", "title"), [
    (["master"], "마스터"),
    (["problem_reviewer", "posts_manager", "problem_author"], "출제진"),
    (["problem_reviewer", "audit_viewer"], "검수진"),
    (["notices_manager"], "운영진"),
    (["problem_reviewer"], "검수진"),
])
def test_staff_title_is_returned_in_submission_and_both_board_views(workspace, roles, title):
    w = workspace
    email = f"titles-{uuid4().hex}@zoj.com"
    store.upsert_contest_operator(w["cid"], email, "손동열", roles)
    submission = store.create_operator_test_submission(w["cid"], w["problem"].problem_id, "cpp17", "int main(){}", submitted_by_email=email, submitted_by_name="손동열")
    answer = store.create_answer(w["cid"], w["question"].contest_question_id, "답변", "public", email)
    assert submission.submitted_by_title == answer.created_by_title == title
    assert submission.submitted_by_name == answer.created_by_name == "손동열"
    for prefix, auth in [("/api/operator", w["master"]), ("/api", w["participant_headers"])]:
        response = client.get(f"{prefix}/contests/{w['cid']}/boards", headers=auth)
        assert response.status_code == 200, response.text
        question = next(item for item in response.json()["data"] if item["contest_question_id"] == w["question"].contest_question_id)
        payload = next(item for item in question["answers"] if item["contest_answer_id"] == answer.contest_answer_id)
        assert payload["created_by_title"] == title
        assert payload["created_by_name"] == "손동열"
    response = client.get(f"/api/operator/contests/{w['cid']}/submissions", headers=w["master"])
    assert response.status_code == 200
    payload = next(item for item in response.json()["data"] if item["submission_id"] == submission.submission_id)
    assert payload["submitted_by_title"] == title
    detail = store.get_submission(submission.submission_id, include_source=False)
    assert detail.submitted_by_title == title
    assert detail.source_code == ""


def test_titles_follow_current_roles_for_the_submission_contest(workspace):
    w = workspace
    email = f"titles-scoped-{uuid4().hex}@zoj.com"
    store.upsert_contest_operator(w["cid"], email, "손동열", ["problem_author"])
    submission = store.create_operator_test_submission(w["cid"], w["problem"].problem_id, "cpp17", "int main(){}", submitted_by_email=email, submitted_by_name="손동열")
    answer = store.create_answer(w["cid"], w["question"].contest_question_id, "답변", "public", email)
    other = store.create_contest("Other", "Test", "Test")
    store.upsert_contest_operator(other.contest_id, email, "손동열", ["master"])
    store.update_contest_operator(w["cid"], email, "손동열", ["problem_reviewer", "notices_manager"])
    assert store.get_submission(submission.submission_id).submitted_by_title == "검수진"
    question = store.get_contest_question(w["cid"], w["question"].contest_question_id)
    assert next(item for item in question.answers if item.contest_answer_id == answer.contest_answer_id).created_by_title == "검수진"


def test_participant_answers_and_submissions_have_no_staff_title(workspace):
    w = workspace
    email = str(w["participant"]["member"].email)
    answer = store.create_answer(w["cid"], w["question"].contest_question_id, "참가자 답변", "public", email)
    submission = store.create_submission(w["cid"], w["problem"].problem_id, email, "cpp17", "int main(){}")
    assert answer.created_by_role == "participant"
    assert answer.created_by_title is None
    assert store.get_submission(submission.submission_id).submitted_by_title is None


def test_owner_titles_follow_delegation_in_existing_answers_and_submissions(workspace):
    w = workspace
    original = next(account for account in store.contest_operator_accounts(w['cid']) if account.contest_roles[w['cid']] == ['owner'])
    recipient = store.upsert_contest_operator(w['cid'], f'next-owner-{uuid4().hex}@zoj.com', '새 총괄', ['problem_author'])
    submissions, answers = [], []
    for account in [original, recipient]:
        submissions.append(store.create_operator_test_submission(w['cid'], w['problem'].problem_id, 'cpp17', 'int main(){}', submitted_by_email=str(account.email), submitted_by_name=account.display_name))
        answers.append(store.create_answer(w['cid'], w['question'].contest_question_id, '총괄 표시 확인', 'public', str(account.email)))
    assert [item.submitted_by_title for item in submissions] == ['총괄', '출제진']
    assert [item.created_by_title for item in answers] == ['총괄', '출제진']
    result = client.post(f"/api/operator/contests/{w['cid']}/owner:transfer", headers=w['master'], json={'email': str(recipient.email)})
    assert result.status_code == 200, result.text
    assert [store.get_submission(item.submission_id).submitted_by_title for item in submissions] == ['마스터', '총괄']
    for prefix, auth in [('/api/operator', w['master']), ('/api', w['participant_headers'])]:
        response = client.get(f"{prefix}/contests/{w['cid']}/boards", headers=auth)
        assert response.status_code == 200
        question = next(item for item in response.json()['data'] if item['contest_question_id'] == w['question'].contest_question_id)
        titles = {answer['contest_answer_id']: answer['created_by_title'] for answer in question['answers']}
        assert [titles[answer.contest_answer_id] for answer in answers] == ['마스터', '총괄']
