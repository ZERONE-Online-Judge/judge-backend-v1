"""Prerequisites shared by test-based judging requests and queue dispatch."""
from sqlalchemy import select

from app.orm_models import TestcaseRow, TestcaseSetRow
from app.services.errors import AppError

TEST_BASED_KINDS = {"participant", "participant_preview", "mock_judging", "operator_test"}
TESTCASES_NOT_READY = "채점할 활성 테스트가 없습니다. 테스트 케이스를 등록하고 활성화한 뒤 다시 요청해 주세요."
QUEUED_TESTCASES_NOT_READY = "채점 대기 중 활성 테스트가 없어 실행하지 못했습니다. 테스트 케이스를 등록하고 활성화한 뒤 다시 요청해 주세요."


def require_active_testcases(db, problem_id):
    ready = db.scalar(select(TestcaseRow.testcase_id).join(
        TestcaseSetRow, TestcaseSetRow.testcase_set_id == TestcaseRow.testcase_set_id,
    ).where(TestcaseSetRow.problem_id == problem_id, TestcaseSetRow.is_active.is_(True)).limit(1))
    if ready is None:
        raise AppError(422, "testcases_not_ready", TESTCASES_NOT_READY)
