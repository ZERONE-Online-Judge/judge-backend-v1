"""Exercise candidate execution through real queue/claim/result transactions."""

import copy
import json
import subprocess
import sys

import pytest
from sqlalchemy import select

from app.models import SubmissionStatus
from app.orm_models import VerificationTrialRow as Trial
from app.services import (
    verification_agent as agent,
    verification_candidates as candidates,
)
from app.services import verification_agent_tools as caps, verification_ai as ai
from app.services.store import store
from test_verification_agent import agent_context, queued, call, judged
from test_verification_ai import context, client, SECRET, REPORT


GOOD = "print(sum(map(int, input().split())))\n"
BAD = "print(7)\n"


def execute_fixture(c):
    """Only these repository-owned tiny fixtures run locally; never user/AI code."""
    jobs = store.claim_jobs(c["node"].judge_node_id, SECRET, 2)
    assert len(jobs) == 1
    job = jobs[0]
    source = job["submission"]["source_code"]
    assert source in {GOOD, BAD}
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        input="2 3\n",
        text=True,
        capture_output=True,
        timeout=3,
        check=True,
    )
    status = (
        SubmissionStatus.ACCEPTED
        if result.stdout.strip() == "5"
        else SubmissionStatus.WRONG_ANSWER
    )
    store.report_judge_result(
        job["judge_job_id"],
        SECRET,
        job["lease_token"],
        status,
        None,
        result.stdout,
        None if status == SubmissionStatus.ACCEPTED else 1,
        10,
        1024,
    )
    return job, status


@pytest.mark.parametrize("tool", ["edit_code", "workspace_candidate"])
@pytest.mark.parametrize("source", [GOOD, BAD])
def test_candidate_automatically_executes_and_checks_real_fixture_output(
    agent_context, tool, source
):
    c = agent_context
    _, row, ctx, state = queued(c)
    if tool == "edit_code":
        command = call(
            tool,
            base_id="original",
            replacements=[{"old": "print(4)\n", "new": source}],
        )
    else:
        agent.handle_tool(
            row, ctx, state, call("workspace_write", path="solution.py", content=source)
        )
        command = call(tool, path="solution.py", language="python311")
    assert agent.handle_tool(row, ctx, state, command) is None
    for _ in range(3):
        # A persisted/restored pending call must retain its candidate identity.
        command = json.loads(json.dumps(command))
        state = json.loads(json.dumps(state))
        assert agent.handle_tool(row, ctx, state, command) is None
    assert len(state["artifacts"]) == 1
    with c["sessions"]() as db:
        runs = db.scalars(select(Trial)).all()
        assert len(runs) == 1 and runs[0].testcase_orders is None
    job, status = execute_fixture(c)
    result = agent.handle_tool(row, ctx, state, command)
    assert result["executions"][0]["status"] == status
    assert result["verification"]["status"] == (
        "passed" if source == GOOD else "failed"
    )
    assert result["verification"]["execution_ids"] == [
        job["submission"]["submission_id"]
    ]
    assert state["calls"] == 0
    assert agent.handle_tool(row, ctx, state, command) == result


def test_registered_pass_replays_known_probe_and_conflict_is_not_certified(
    agent_context,
):
    c = agent_context
    _, row, ctx, state = queued(c)
    probe = call(
        "run_probe", artifact_id="original", input="-1 1\n", expected_output="9\n"
    )
    assert agent.handle_tool(row, ctx, state, probe) is None
    judged(c, SubmissionStatus.WRONG_ANSWER)
    command = call(
        "edit_code",
        base_id="original",
        replacements=[{"old": "print(4)\n", "new": GOOD}],
    )
    assert agent.handle_tool(row, ctx, state, command) is None
    execute_fixture(c)
    assert agent.handle_tool(row, ctx, state, command) is None  # auto probe replay
    job = judged(c, SubmissionStatus.WRONG_ANSWER)
    assert job["testcases"][0]["input_text"] == "-1 1\n"
    result = agent.handle_tool(row, ctx, state, command)
    assert result["verification"]["registered_passed"] is True
    assert result["verification"]["status"] == "inconclusive"
    assert result["verification"]["probe_conflicts"] == 1
    assert result["verification"]["unreplayed_probes"] == 0
    assert "독립 검토" in result["verification"]["message"]
    state["task_goal"] = "고쳐 주세요"
    state["plan"] = [{"title": "검증", "status": "done"}]
    agent.handle_tool(
        row, ctx, state, call("finish_task", outcome="completed", report=REPORT)
    )
    assert state["outcome"] == "inconclusive"


def test_failed_candidate_requires_new_code_and_does_not_reuse_old_pass(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    good = call(
        "edit_code",
        base_id="original",
        replacements=[{"old": "print(4)\n", "new": GOOD}],
    )
    assert agent.handle_tool(row, ctx, state, good) is None
    execute_fixture(c)
    agent.handle_tool(row, ctx, state, good)
    bad = call(
        "edit_code", base_id="candidate-1", replacements=[{"old": GOOD, "new": BAD}]
    )
    assert agent.handle_tool(row, ctx, state, bad) is None
    execute_fixture(c)
    result = agent.handle_tool(row, ctx, state, bad)
    assert result["verification"]["status"] == "failed"
    # Even a model claiming success cannot certify unexecuted or failing code.
    report = copy.deepcopy(REPORT)
    report["fixes"][0].update(code_example=BAD, verification="모두 통과")
    report = agent.final_report(row, state, report)
    assert "실패" in report["fixes"][0]["verification"]
    assert "실패" in report["verdict_assessment"]
    assert state["phase"] == "일부 검증 후 종료"
    with c["sessions"]() as db:
        runs = caps.results(db, row.analysis_id)
    assert (
        candidates.assessment(
            row, {**state["artifacts"]["candidate-1"], "language": "cpp17"}, runs
        )["status"]
        == "unverified"
    )
    changed = copy.copy(row)
    changed.context_hash = "another-snapshot"
    assert (
        candidates.assessment(changed, state["artifacts"]["candidate-1"], runs)[
            "status"
        ]
        == "unverified"
    )


def test_execution_limit_returns_unverified_candidate_without_exceeding_quota(
    agent_context,
):
    c = agent_context
    _, row, ctx, state = queued(c)
    state["limits"]["max_runs"] = 1
    agent.handle_tool(
        row, ctx, state, call("run_code", artifact_id="original", testcase_orders=[])
    )
    judged(c, SubmissionStatus.WRONG_ANSWER)
    result = agent.handle_tool(
        row,
        ctx,
        state,
        call(
            "edit_code",
            base_id="original",
            replacements=[{"old": "print(4)\n", "new": GOOD}],
        ),
    )
    assert result["verification"]["status"] == "unverified"
    assert "한도" in result["execution_error"]
    with c["sessions"]() as db:
        assert len(db.scalars(select(Trial)).all()) == 1


def test_candidate_identical_to_previous_code_uses_exact_cached_execution(
    agent_context,
):
    c = agent_context
    _, row, ctx, state = queued(c)
    command = call(
        "edit_code",
        base_id="original",
        replacements=[{"old": "print(4)\n", "new": GOOD}],
    )
    agent.handle_tool(row, ctx, state, command)
    execute_fixture(c)
    first = agent.handle_tool(row, ctx, state, command)
    second = agent.handle_tool(
        row,
        ctx,
        state,
        call(
            "edit_code",
            base_id="original",
            replacements=[{"old": "print(4)\n", "new": GOOD}],
        ),
    )
    assert second["artifact_id"] == "candidate-2"
    assert second["verification"] == first["verification"]
    assert len(store.claim_jobs(c["node"].judge_node_id, SECRET, 2)) == 0


def test_code_fragments_and_selected_passes_are_not_full_verification(agent_context):
    _, row, ctx, state = queued(agent_context)
    files = caps.make_manifest(ctx, row.evidence, state["references"])
    caps.edit_code(
        state, files, "python311", "original", [{"old": "print(4)\n", "new": GOOD}]
    )
    caps.run_code(row, state, files, ctx, "candidate-1", [1])
    judged(agent_context, SubmissionStatus.ACCEPTED)
    report = agent.final_report(row, state, REPORT)
    assert "실행해 검증한 기록은 없습니다" in report["fixes"][0]["verification"]
    assert "전체 등록 테스트 통과를 확인하지 못했습니다" in report["verdict_assessment"]


def test_full_pass_with_probe_blocked_by_limit_remains_inconclusive(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    state["limits"]["max_runs"] = 2
    agent.handle_tool(
        row,
        ctx,
        state,
        call(
            "run_probe", artifact_id="original", input="-1 1\n", expected_output="0\n"
        ),
    )
    judged(c, SubmissionStatus.WRONG_ANSWER)
    command = call(
        "edit_code",
        base_id="original",
        replacements=[{"old": "print(4)\n", "new": GOOD}],
    )
    assert agent.handle_tool(row, ctx, state, command) is None
    execute_fixture(c)
    result = agent.handle_tool(row, ctx, state, command)
    assert result["verification"]["registered_passed"]
    assert result["verification"]["status"] == "inconclusive"
    assert result["verification"]["unreplayed_probes"] == 1
    assert "한도" in result["execution_error"]
    assert state["calls"] == 0


def test_system_error_never_proves_candidate_wrong_or_correct(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    command = call(
        "edit_code",
        base_id="original",
        replacements=[{"old": "print(4)\n", "new": GOOD}],
    )
    assert agent.handle_tool(row, ctx, state, command) is None
    judged(c, SubmissionStatus.SYSTEM_ERROR)
    result = agent.handle_tool(row, ctx, state, command)
    assert result["verification"]["status"] == "inconclusive"
    assert not result["verification"]["registered_passed"]
    assert result["executions"][0]["status"] == "system_error"
