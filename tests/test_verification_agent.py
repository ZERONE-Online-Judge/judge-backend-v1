"""Exercise the tool loop through actual submission/claim/report transactions."""

import copy
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import SubmissionStatus, now_utc
from app.orm_models import (
    JudgeJobRow,
    ProblemRow,
    SubmissionRow,
    VerificationTrialRow as Trial,
    TestcaseRow as Case,
)
from app.services import (
    verification_ai as ai,
    verification_agent as agent,
    verification_agent_tools as caps,
)
from app.services.store import store
from app.settings import settings
from test_verification_ai import (
    context,
    submit,
    client,
    SECRET,
    REPORT,
    request_completed,
)


@pytest.fixture
def agent_context(context, monkeypatch):
    monkeypatch.setattr(settings, "verification_agent_enabled", True)
    for key, value in {
        "model": "gpt-5.4-mini",
        "max_cost_usd": 0.20,
        "max_calls": 10,
        "max_tools": 48,
        "max_runs": 6,
        "max_playground_runs": 12,
        "timeout_seconds": 900,
    }.items():
        monkeypatch.setattr(settings, "verification_agent_" + key, value)
    return context


def queued(c):
    sid = submit(c)
    request_completed(c)
    with c["sessions"]() as db:
        row = db.scalar(select(ai.Analysis))
        snapshot = db.get(ai.Snapshot, row.context_hash)
        state = agent.initial_state(db, row, snapshot.context)
        row.started_at = now_utc()
        return sid, row, snapshot.context, state


def call(name, **args):
    return {
        "type": "function_call",
        "call_id": "call_" + name,
        "name": name,
        "arguments": json.dumps(args),
    }


def ready(c):
    with c["sessions"]() as db:
        db.execute(
            update(ai.Analysis).values(next_step_at=now_utc() - timedelta(seconds=1))
        )
        db.commit()


def judged(c, expected, *, source=None):
    jobs = store.claim_jobs(c["node"].judge_node_id, SECRET, 2)
    assert len(jobs) == 1
    job = jobs[0]
    if source:
        assert job["submission"]["source_code"] == source
    store.report_judge_result(
        job["judge_job_id"],
        SECRET,
        job["lease_token"],
        expected,
        None,
        (
            "testcase #1: expected 5, actual 4"
            if expected != SubmissionStatus.ACCEPTED
            else ""
        ),
        1 if expected != SubmissionStatus.ACCEPTED else None,
        10,
        1024,
        **({"testcase_results": [{"index": case["display_order"], "final_status": expected.value, "runtime_ms": 10, "memory_kb": 1024} for case in job["testcases"]]} if job.get("collect_all_results") else {}),
    )
    ready(c)
    return job


def test_tool_loop_persists_waits_runs_original_and_candidate_and_shares_report(
    agent_context, monkeypatch
):
    c = agent_context
    sid, row, ctx, state = queued(c)
    requests = []
    batches = [
        [
            call("read_file", file_id="problem", offset=0, length=4000),
            call("read_file", file_id="original", offset=0, length=4000),
            call("read_file", file_id="case:1:output", offset=0, length=1000),
            call("run_code", artifact_id="original", testcase_orders=[1]),
        ],
        [
            call(
                "edit_code",
                base_id="original",
                replacements=[
                    {"old": "print(4)", "new": "print(sum(map(int, input().split())))"}
                ],
            )
        ],
        [
            call(
                "finish_report",
                **{
                    **REPORT,
                    "limitations": [],
                    "summary": "원본 WA를 재현했고 수정 후보가 등록 테스트를 통과했습니다.",
                },
            )
        ],
    ]

    def provider(state, output_budget):
        requests.append(copy.deepcopy(state))
        assert 1024 <= output_budget <= 4096
        return {
            "status": "completed",
            "output": batches.pop(0),
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 100},
            },
        }

    monkeypatch.setattr(agent, "request_model", provider)
    assert agent.process_one()  # model plans tools, no giant source/test context
    assert "print(4)" not in requests[0]["history"][0]["content"]
    assert agent.process_one()  # tools & enqueue original
    with c["sessions"]() as db:
        assert db.scalar(select(Trial)).testcase_orders == [1]
    for _ in range(3):
        ready(c)
        assert agent.process_one()  # poll judge, no model calls or extra jobs
    assert len(requests) == 1
    with c["sessions"]() as db:
        assert len(db.scalars(select(Trial)).all()) == 1
    original = judged(c, SubmissionStatus.WRONG_ANSWER)
    assert original["bundle_url"] is None and len(original["testcases"]) == 1
    assert agent.process_one()  # deliver result
    assert agent.process_one()  # edit plan
    assert agent.process_one()  # edit automatically enqueues full judge run
    for _ in range(3):
        ready(c)
        assert agent.process_one()  # pending edit never recreates code or calls model
    assert len(requests) == 2
    fixed = judged(
        c, SubmissionStatus.ACCEPTED, source="print(sum(map(int, input().split())))\n"
    )
    assert fixed["submission"]["submission_kind"] == "verification_trial"
    assert agent.process_one()  # actual verdict tool output
    assert agent.process_one()  # report plan
    assert agent.process_one()  # finish
    result = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]
    assert result["status"] == "succeeded" and result["engine_version"] == 2
    assert [r["status"] for r in result["executions"]] == ["wrong_answer", "accepted"]
    assert result["artifacts"][0]["source"] == fixed["submission"]["source_code"]
    assert (
        result["usage"]["input_tokens"] == 1500
        and result["usage"]["estimated_cost_usd"] < 0.01
    )
    assert result["artifacts"][0]["verification"]["status"] == "passed"
    edit_result = json.loads(
        next(
            item["output"]
            for item in requests[-1]["history"]
            if item.get("type") == "function_call_output"
            and item["call_id"] == "call_edit_code"
        )
    )
    assert edit_result["executions"][0]["status"] == "accepted"
    assert (
        "history" not in result
        and "references" not in result
        and "agent_state" not in result
    )
    with c["sessions"]() as db:
        saved = db.get(ai.Analysis, row.analysis_id)
        assert "history" not in saved.agent_state
    assert store.get_submission(fixed["submission"]["submission_id"]) is None
    assert all(
        s.submission_kind != "verification_trial"
        for s in store.list_submissions(contest_id=c["cid"])[0]
    )
    before = len(requests)
    assert (
        ai.request_analysis(c["cid"], c["pid"], sid)["analysis"]["status"]
        == "succeeded"
    )
    assert len(requests) == before


def test_scoped_file_tools_and_exact_edits(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    files = caps.make_manifest(ctx, row.evidence, state["references"])
    for bad in ("../../etc/passwd", "https://example.com", "asset:another-contest"):
        with pytest.raises(caps.ToolError):
            caps.file_bytes(files, bad)
    with pytest.raises(caps.ToolError):
        caps.edit_code(
            state, files, "python311", "original", [{"old": "absent", "new": "x"}]
        )
    assert state["artifacts"] == {}
    read = caps.read_file(files, "original", 0, 4)
    assert read["text"] == "prin" and not read["complete"] and read["next_offset"] == 4
    assert caps.search_file(files, "original", "print")["matches"][0]["offset"] == 0
    from app.services.storage import object_storage

    object_storage.write_bytes(c["keys"]["input"], b"changed")
    with pytest.raises(caps.ToolError, match="체크섬"):
        caps.file_bytes(files, "case:1:input")


def test_changed_context_is_rejected_at_enqueue_and_claim(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    files = caps.make_manifest(ctx, row.evidence, state["references"])
    result = caps.run_code(row, state, files, ctx, "original", [1])
    with c["sessions"]() as db:
        db.get(ProblemRow, c["pid"]).statement = "새로운 문제"
        db.commit()
    assert store.claim_jobs(c["node"].judge_node_id, SECRET, 2) == []
    with c["sessions"]() as db:
        assert db.get(SubmissionRow, result["submission_id"]).status == "system_error"
    with pytest.raises(caps.ToolError, match="변경"):
        caps.run_code(row, state, files, ctx, "original", [])


def test_human_submissions_have_priority_and_trial_is_idempotent(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    files = caps.make_manifest(ctx, row.evidence, state["references"])
    one = caps.run_code(row, state, files, ctx, "original", [])
    two = caps.run_code(row, state, files, ctx, "original", [])
    assert one["submission_id"] == two["submission_id"]
    human = store.create_operator_test_submission(
        c["cid"], c["pid"], "python311", "print(42)"
    )
    jobs = store.claim_jobs(c["node"].judge_node_id, SECRET, 1)
    assert jobs[0]["submission"]["submission_id"] == human.submission_id
    with c["sessions"]() as db:
        assert len(db.scalars(select(Trial)).all()) == 1


def test_cost_and_tool_limits_do_not_call_provider(agent_context, monkeypatch):
    _, row, ctx, state = queued(agent_context)
    monkeypatch.setattr(agent, "request_model", lambda *_: pytest.fail("must not bill"))
    state["limits"]["max_cost_usd"] = 0.00001
    agent.step(row, ctx, state)
    assert "예산" in state["report"]["limitations"][0]
    assert state["usage"]["total_tokens"] == 0
    _, row, ctx, state = queued(agent_context)
    state["pending"] = [call("read_file", file_id="original", offset=0, length=200)]
    state["tools"] = state["limits"]["max_tools"]
    agent.step(row, ctx, state)
    assert "도구" in state["report"]["limitations"][0]


def test_cache_ignores_non_resource_timing_noise(agent_context, monkeypatch):
    c = agent_context
    sid, row, _, _ = queued(c)
    with c["sessions"]() as db:
        sub = db.get(SubmissionRow, sid)
        sub.runtime_ms, sub.memory_kb = 11, 1050
        run = db.get(ai.Run, sid)
        assert ai._queue(db, run, sub).analysis_id == row.analysis_id
        run.expected_status = "time_limit_exceeded"
        resource = ai._queue(db, run, sub).analysis_id
        sub.runtime_ms = 1200
        assert ai._queue(db, run, sub).analysis_id != resource
        current = ai._queue(db, run, sub).analysis_id
        monkeypatch.setattr(agent, "PROMPT_VERSION", "test-new-agent-version")
        assert ai._queue(db, run, sub).analysis_id != current


def test_escalation_requires_actual_evidence_and_budget(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    reason = "원본을 재실행했으나 공식 기대 출력과 문제 해설의 계산 결과가 모순됩니다."
    with pytest.raises(caps.ToolError, match="실제 실행"):
        agent.handle_tool(row, ctx, state, call("escalate", reason=reason))
    files = caps.make_manifest(ctx, row.evidence, state["references"])
    caps.run_code(row, state, files, ctx, "original", [])
    judged(c, SubmissionStatus.WRONG_ANSWER)
    assert agent.handle_tool(row, ctx, state, call("escalate", reason=reason))[
        "switched"
    ]
    assert state["model"] == "gpt-5.4"
    with pytest.raises(caps.ToolError):
        agent.handle_tool(row, ctx, state, call("escalate", reason=reason))


def test_finish_requires_actual_original_execution(agent_context):
    _, row, ctx, state = queued(agent_context)
    with pytest.raises(caps.ToolError, match="원본 재실행"):
        agent.handle_tool(row, ctx, state, call("finish_report", **REPORT))


def test_agent_reports_are_resource_permission_scoped(agent_context):
    c = agent_context
    sid, _, _, _ = queued(c)
    url = c["base"] + f"/verification-runs/{sid}/analysis"
    assert client.get(url, headers=c["headers"]["reviewer"]).status_code == 403
    assert client.post(url, headers=c["headers"]["reviewer"]).status_code == 403


def test_incomplete_response_usage_is_saved_and_not_automatically_rebilled(
    agent_context, monkeypatch
):
    c = agent_context
    sid, _, _, _ = queued(c)
    monkeypatch.setattr(
        agent,
        "request_model",
        lambda *_: {
            "status": "incomplete",
            "usage": {"input_tokens": 1000, "output_tokens": 4096},
        },
    )
    assert agent.process_one() and not agent.process_one()
    result = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]
    assert result["status"] == "succeeded" and result["usage"]["output_tokens"] == 4096
    assert "완성되지" in result["report"]["limitations"][0]


def test_probe_uses_isolated_inline_input_and_marks_hypothetical_oracle(agent_context):
    c = agent_context
    _, row, ctx, state = queued(c)
    probe_call = call(
        "run_probe", artifact_id="original", input="-1 1\n", expected_output="0\n"
    )
    assert agent.handle_tool(row, ctx, state, probe_call) is None
    job = store.claim_jobs(c["node"].judge_node_id, SECRET, 1)[0]
    assert job["bundle_url"] is None
    assert len(job["testcases"]) == 1
    assert job["testcases"][0]["input_text"] == "-1 1\n"
    assert job["testcases"][0]["output_text"] == "0\n"
    with c["sessions"]() as db:
        assert len(db.scalars(select(Case)).all()) == 1
        trial = db.scalar(select(Trial))
        assert trial.probe["expected_output_source"] == "AI hypothesis"
        assert not trial.probe["validator_checked"]


def test_measured_prompt_prefix_preserves_budget_for_final_report(
    agent_context, monkeypatch
):
    _, row, ctx, state = queued(agent_context)
    monkeypatch.setattr(
        agent,
        "request_model",
        lambda *_: {
            "status": "completed",
            "output": [call("read_file", file_id="original", offset=0, length=200)],
            "usage": {"input_tokens": 3000, "output_tokens": 100},
        },
    )
    agent.step(row, ctx, state)
    agent.step(row, ctx, state)
    # Measured prefixes reduce the input cost reserved for the next request.
    state["usage"]["input_tokens"] = 52000
    assert agent.budget_for_request(state) is not None
    assert 3000 < state["request_input_bound"] < 8000
    measured = state["request_input_bound"]
    # Leave just enough cost for the measured prefix and a minimum response.
    a, _, c = agent.cost_rates(state["model"], measured)
    state["limits"]["max_cost_usd"] = (
        state["usage"]["estimated_cost_usd"]
        + (measured * a + 1025 * c) / 1_000_000
    )
    assert agent.budget_for_request(state) is not None
    state["history"][0]["content"] += "자료 변경"
    assert agent.budget_for_request(state) is None
    assert state["budget_blocked"]["code"] == "cost"
    assert state["request_input_bound"] > measured
    state["history"][0]["content"] = state["history"][0]["content"][:-5]
    state["model"] = "gpt-5.4"
    assert agent.budget_for_request(state) is None


def test_network_response_loss_reserves_budget_before_manual_retry(
    agent_context, monkeypatch
):
    from app.services.errors import AppError

    c = agent_context
    sid, _, _, _ = queued(c)

    def failed(*_):
        raise AppError(503, "verification_agent_network", "응답 유실")

    monkeypatch.setattr(agent, "request_model", failed)
    assert agent.process_one()
    result = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]
    assert result["status"] == "failed"
    assert result["usage"]["includes_unconfirmed_request"]
    assert result["usage"]["estimated_cost_usd"] > 0
    assert not agent.process_one()


def test_workspace_zip_is_shared_but_permission_scoped(agent_context):
    import base64, io, zipfile

    c = agent_context
    sid, row, _, _ = queued(c)
    with c["sessions"]() as db:
        saved = db.get(ai.Analysis, row.analysis_id)
        saved.agent_state = {
            "workspace": {"src/main.py": base64.b64encode(b"print(5)\n").decode()},
            "history": [{"private": "must not export"}],
        }
        db.commit()
    path = c["base"] + f"/verification-runs/{sid}/workspace.zip"
    assert client.get(path, headers=c["headers"]["reviewer"]).status_code == 403
    response = client.get(path, headers=c["headers"]["owner"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["src/main.py"]
        assert archive.read("src/main.py") == b"print(5)\n"
