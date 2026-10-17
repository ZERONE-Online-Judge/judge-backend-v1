import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import app
import app.main as main_module
from app.models import ContestStatus, SubmissionStatus, now_utc
from app.orm_models import (
    ProblemAssetRow,
    ProblemRow,
    SubmissionRow,
    TestcaseRow as Case,
    TestcaseSetRow as CaseSet,
)
from app.services import verification_ai as ai
from app.services.store import store
from app.services.storage import object_storage
from app.settings import settings

client = TestClient(app)
SECRET = "verification-agent-secret-with-more-than-32-characters"
REPORT = {
    "summary": "경계조건에서 합을 잘못 계산합니다.",
    "verdict_assessment": "오답 판정이 코드와 일치합니다.",
    "causes": [
        {
            "title": "합 계산 누락",
            "confidence": "high",
            "evidence": "테스트 #1 입력 2 3, 기대 출력 5",
            "explanation": "코드가 4를 출력합니다.",
            "code_reference": "main.py:1",
        }
    ],
    "fixes": [
        {
            "title": "입력 합산",
            "change": "입력받은 두 수를 더합니다.",
            "code_example": "print(sum(map(int,input().split())))",
            "verification": "2 3과 음수 입력으로 다시 채점합니다.",
        }
    ],
    "suggested_tests": [
        {
            "input": "-1 1\n",
            "expected_output": "0\n",
            "explanation": "직접 실행하지 않은 경계조건 제안입니다.",
        }
    ],
    "limitations": ["정적 검토이며 코드를 실행하지 않았습니다."],
}


@pytest.fixture
def context(tmp_path, monkeypatch):
    postgres_url = os.environ.get("TEST_VERIFICATION_AI_DATABASE_URL")
    schema = "verification_test_" + uuid4().hex
    if postgres_url:
        admin = create_engine(postgres_url)
        with admin.begin() as db:
            db.execute(text(f"CREATE SCHEMA {schema}"))
        engine = create_engine(
            postgres_url, connect_args={"options": f"-csearch_path={schema}"}
        )
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path}/ai.db", connect_args={"check_same_thread": False}
        )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(store, "_session", sessions)
    monkeypatch.setattr(ai, "SessionLocal", sessions)
    monkeypatch.setattr(main_module, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-key-not-real"))
    monkeypatch.setattr(settings, "verification_ai_daily_limit", 50)
    monkeypatch.setattr(settings, "verification_agent_enabled", False)
    calls = []

    def provider(model, content):
        calls.append((model, content))
        return REPORT, {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300}

    monkeypatch.setattr(ai, "call_openai", provider)
    contest = store.create_contest(
        "AI 검토", "ZOJ", "", now_utc() + timedelta(days=2), status=ContestStatus.OPEN
    )
    cid = contest.contest_id
    headers = {}
    for role, name in [
        ("master", "owner"),
        ("problem_author", "author"),
        ("problem_reviewer", "reviewer"),
    ]:
        email = f"{name}-{uuid4().hex}@example.com"
        store.upsert_contest_operator(cid, email, name, [role])
        result = TestClient(app).post(
            "/api/auth/general/otp/verify",
            json={"email": email, "otp_code": "", "force_new_session": True},
        )
        assert result.status_code == 200, result.text
        headers[name] = {
            "Authorization": "Bearer "
            + result.json()["data"]["operator_session"]["access_token"]
        }
    division = store.create_contest_division(cid, "A", "일반부")
    problem = store.create_problem(
        cid,
        division.division_id,
        "A",
        "두 수의 합",
        "두 정수의 합을 출력하시오.",
        1000,
        256,
        {},
        1,
    )
    pid = problem.problem_id
    source = "print(4)\n"
    key = f"contests/{cid}/problems/{pid}/verification-solutions/accepted/main.py"
    keys = {
        "source": key,
        "input": f"contests/{cid}/problems/{pid}/case.in",
        "output": f"contests/{cid}/problems/{pid}/case.out",
    }
    for name, data in [
        ("source", source.encode()),
        ("input", b"2 3\n"),
        ("output", b"5\n"),
    ]:
        object_storage.write_bytes(keys[name], data)
    aid, sid = str(uuid4()), str(uuid4())
    with sessions() as db:
        db.add(
            ProblemAssetRow(
                asset_id=aid,
                contest_id=cid,
                problem_id=pid,
                original_filename="main.py",
                storage_key=key,
                mime_type="text/plain",
                file_size=len(source),
                sha256=hashlib.sha256(source.encode()).hexdigest(),
                asset_status="active",
            )
        )
        db.add(CaseSet(testcase_set_id=sid, problem_id=pid, version=1, is_active=True))
        db.flush()
        db.add(
            Case(
                testcase_set_id=sid,
                display_order=1,
                input_storage_key=keys["input"],
                output_storage_key=keys["output"],
                input_sha256=hashlib.sha256(b"2 3\n").hexdigest(),
                output_sha256=hashlib.sha256(b"5\n").hexdigest(),
            )
        )
        db.commit()
    node = store.provision_node("test-agent", SECRET, 2)
    from app.orm_models import JudgeNodeRow
    with sessions() as db:
        db.get(JudgeNodeRow, node.judge_node_id).execution_capabilities = ["zoj-judge-full-v1"]
        db.commit()
    yield dict(
        cid=cid,
        pid=pid,
        aid=aid,
        source=source,
        headers=headers,
        sessions=sessions,
        node=node,
        calls=calls,
        keys=keys,
        base=f"/api/operator/contests/{cid}/problems/{pid}",
    )
    engine.dispose()
    if postgres_url:
        with admin.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def submit(c, *, expected_status=SubmissionStatus.WRONG_ANSWER, claim=True):
    response = client.post(
        c["base"] + "/test-submissions",
        headers=c["headers"]["owner"],
        json={
            "language": "python313",
            "source_code": c["source"],
            "verification_asset_id": c["aid"],
        },
    )
    assert response.status_code == 200, response.text
    sid = response.json()["data"]["submission_id"]
    if claim:
        jobs = store.claim_jobs(c["node"].judge_node_id, SECRET, 2)
        job = next(job for job in jobs if job["submission"]["submission_id"] == sid)
        store.report_judge_result(
            job["judge_job_id"],
            SECRET,
            job["lease_token"],
            expected_status,
            None,
            "testcase #1: wrong answer\n[input]\n2 3\n[expected]\n5\n[actual]\n4",
            None if expected_status == SubmissionStatus.ACCEPTED else 1,
            10,
            1024,
            testcase_results=[{"index": 1, "final_status": expected_status.value, "runtime_ms": 10, "memory_kb": 1024}] if expected_status not in {SubmissionStatus.COMPILE_ERROR, SubmissionStatus.SYSTEM_ERROR} else None,
        )
    return sid


def request_completed(c):
    """Explicitly request eligible fixture runs, as an operator button would."""
    if not ai.enabled():
        return
    with c["sessions"]() as db:
        ids = db.scalars(
            select(ai.Run.submission_id)
            .join(SubmissionRow, ai.Run.submission_id == SubmissionRow.submission_id)
            .where(
                ai.Run.analysis_id.is_(None),
                SubmissionRow.status.not_in(ai.PENDING),
                SubmissionRow.status != ai.Run.expected_status,
            )
        ).all()
    for sid in ids:
        ai.request_analysis(c["cid"], c["pid"], sid)


def test_completed_mismatch_and_views_never_start_an_unrequested_analysis(context):
    c = context
    sid = submit(c)
    for _ in range(3):
        assert ai.list_runs(c["cid"], c["pid"])["runs"][0]["analysis"] is None
        assert ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"] is None
        assert not ai.process_one()
    assert not c["calls"]
    response = client.post(
        c["base"] + f"/verification-runs/{sid}/analysis", headers=c["headers"]["owner"]
    )
    assert response.status_code == 200
    with c["sessions"]() as db:
        assert db.scalar(select(ai.Analysis)).requested_at is not None
    assert ai.process_one() and len(c["calls"]) == 1


def test_requested_report_is_shared_persistent_and_cached(context):
    c = context
    sid = submit(c)
    request_completed(c)
    request_completed(c)
    assert ai.process_one() and not ai.process_one()
    for role in ("owner", "author"):
        response = client.get(
            c["base"] + "/verification-runs", headers=c["headers"][role]
        )
        assert response.status_code == 200, response.text
        run = response.json()["data"]["runs"][0]
        assert (
            run["submission"]["submission_id"] == sid
            and run["analysis"]["status"] == "succeeded"
        )
        response = client.get(
            c["base"] + f"/verification-runs/{sid}/analysis", headers=c["headers"][role]
        )
        assert response.json()["data"]["analysis"]["report"] == REPORT
    assert len(c["calls"]) == 1
    cached = client.post(
        c["base"] + f"/verification-runs/{sid}/analysis", headers=c["headers"]["author"]
    )
    assert cached.status_code == 200 and len(c["calls"]) == 1
    evidence = json.loads(c["calls"][0][1][0]["text"])
    assert evidence["problem"]["statement"] == "두 정수의 합을 출력하시오."
    assert evidence["verification"]["source_code"] == "print(4)\n"
    assert evidence["testcases"][0]["input"] == "2 3\n"
    assert evidence["testcases"][0]["expected_output"] == "5\n"
    assert (
        evidence["coverage"]["full_testcases"] == 1
        and not evidence["coverage"]["partial"]
    )
    assert "submitted_by_email" not in json.dumps(evidence)


def test_claim_snapshot_does_not_use_edited_problem_or_testcase_version(context):
    c = context
    sid = submit(c)
    with c["sessions"]() as db:
        db.get(ProblemRow, c["pid"]).statement = "나중에 바뀐 문제"
        db.commit()
    request_completed(c)
    ai.process_one()
    evidence = json.loads(c["calls"][0][1][0]["text"])
    assert evidence["problem"]["statement"] == "두 정수의 합을 출력하시오."
    assert ai.list_runs(c["cid"], c["pid"])["runs"][0]["stale"] is True
    second = submit(c)
    request_completed(c)
    ai.process_one()
    assert len(c["calls"]) == 2
    assert (
        json.loads(c["calls"][1][1][0]["text"])["problem"]["statement"]
        == "나중에 바뀐 문제"
    )


def test_identical_rerun_reuses_the_same_report(context):
    c = context
    first = submit(c)
    request_completed(c)
    ai.process_one()
    second = submit(c)
    request_completed(c)
    assert not ai.process_one() and len(c["calls"]) == 1
    assert (
        ai.analysis_detail(c["cid"], c["pid"], first)["analysis"]["analysis_id"]
        == ai.analysis_detail(c["cid"], c["pid"], second)["analysis"]["analysis_id"]
    )


def test_permission_and_contest_boundaries_do_not_leak_hidden_tests(context):
    c = context
    sid = submit(c)
    request_completed(c)
    ai.process_one()
    path = c["base"] + f"/verification-runs/{sid}/analysis"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=c["headers"]["reviewer"]).status_code == 403
    assert client.post(path, headers=c["headers"]["reviewer"]).status_code == 403
    other = store.create_contest(
        "다른 대회", "ZOJ", "", now_utc() + timedelta(days=2), status=ContestStatus.OPEN
    )
    assert (
        client.get(
            path.replace(c["cid"], other.contest_id), headers=c["headers"]["owner"]
        ).status_code
        == 403
    )
    assert (
        client.get(
            path.replace(c["pid"], str(uuid4())), headers=c["headers"]["owner"]
        ).status_code
        == 404
    )


def test_linked_submission_rejects_mismatched_source_and_unprivileged_link(context):
    c = context
    payload = {
        "language": "python313",
        "source_code": "print(100)",
        "verification_asset_id": c["aid"],
    }
    assert (
        client.post(
            c["base"] + "/test-submissions", headers=c["headers"]["owner"], json=payload
        ).status_code
        == 409
    )
    payload["source_code"] = c["source"]
    assert (
        client.post(
            c["base"] + "/test-submissions",
            headers=c["headers"]["reviewer"],
            json=payload,
        ).status_code
        == 403
    )
    with c["sessions"]() as db:
        assert db.scalar(select(func.count()).select_from(ai.Run)) == 0


def test_expected_verdict_and_unfinished_runs_never_trigger_ai(context):
    c = context
    submit(c, expected_status=SubmissionStatus.ACCEPTED)
    pending = submit(c, claim=False)
    request_completed(c)
    assert not ai.process_one()
    assert (
        client.post(
            c["base"] + f"/verification-runs/{pending}/analysis",
            headers=c["headers"]["owner"],
        ).status_code
        == 409
    )
    assert not c["calls"]


def test_disabled_provider_keeps_saved_reports_readable(context, monkeypatch):
    c = context
    sid = submit(c)
    request_completed(c)
    ai.process_one()
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert not ai.enabled()
    result = ai.analysis_detail(c["cid"], c["pid"], sid)
    assert result["analysis"]["report"] == REPORT and not result["available"]
    assert (
        ai.request_analysis(c["cid"], c["pid"], sid)["analysis"]["status"]
        == "succeeded"
    )
    submit(c)
    request_completed(c)
    assert not ai.process_one()
    assert len(c["calls"]) == 1


def test_partial_missing_and_changed_files_are_disclosed(context, monkeypatch):
    c = context
    sid = submit(c)
    object_storage.write_bytes(c["keys"]["input"], b"9 9\n")
    object_storage.delete(c["keys"]["output"])
    request_completed(c)
    ai.process_one()
    coverage = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]["coverage"]
    assert coverage["partial"] and coverage["omitted_testcases"] == 1
    assert not json.loads(c["calls"][0][1][0]["text"])["testcases"]


def test_daily_cap_and_failed_request_require_bounded_retry(context, monkeypatch):
    c = context
    sid = submit(c)
    request_completed(c)

    def fail(*args):
        raise RuntimeError("secret-key-and-provider-body")

    monkeypatch.setattr(ai, "call_openai", fail)
    assert ai.process_one()
    report = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]
    assert report["status"] == "failed" and "secret" not in report["error_message"]
    assert not ai.process_one()
    monkeypatch.setattr(settings, "verification_ai_daily_limit", 1)
    ai.request_analysis(c["cid"], c["pid"], sid)
    assert not ai.process_one()


def test_problem_deletion_removes_saved_reports(context):
    c = context
    submit(c)
    request_completed(c)
    ai.process_one()
    store.delete_problem(c["cid"], c["pid"])
    with c["sessions"]() as db:
        for model in (ai.Run, ai.Snapshot, ai.Analysis):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_responses_request_is_stateless_structured_and_parsed(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-key"))
    seen = []

    def post(url, **kwargs):
        seen.append(kwargs)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(REPORT)}
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            },
        )

    monkeypatch.setattr(ai.httpx, "post", post)
    report, usage = ai.call_openai(
        "gpt-5.4", [{"type": "input_text", "text": "untrusted fixture"}]
    )
    assert report == REPORT and usage["total_tokens"] == 30
    assert (
        seen[0]["json"]["store"] is False
        and seen[0]["json"]["text"]["format"]["strict"] is True
    )
    assert seen[0]["json"]["input"][0]["role"] == "user"
    assert "test-key" not in json.dumps(seen[0]["json"])


@pytest.mark.parametrize(
    "status,payload",
    [
        (429, {"secret": "hidden"}),
        (200, {"status": "incomplete"}),
        (200, {"status": "completed", "output": []}),
    ],
)
def test_provider_error_and_incomplete_output_are_not_cached_as_success(
    monkeypatch, status, payload
):
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("test-key"))
    monkeypatch.setattr(
        ai.httpx, "post", lambda *args, **kwargs: httpx.Response(status, json=payload)
    )
    with pytest.raises(ai.AppError) as error:
        ai.call_openai("gpt-5.4", [])
    assert "hidden" not in error.value.message


def test_concurrent_workers_make_only_one_provider_call(context):
    from concurrent.futures import ThreadPoolExecutor

    c = context
    submit(c)
    request_completed(c)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ai.process_one(), range(4)))
    assert results.count(True) == 1 and len(c["calls"]) == 1


def test_deleted_verification_cancels_unstarted_provider_request(context):
    c = context
    sid = submit(c)
    request_completed(c)
    assert store.delete_problem_asset(c["cid"], c["pid"], c["aid"])
    assert not ai.process_one() and not c["calls"]
    assert ai.list_runs(c["cid"], c["pid"])["runs"] == []
    assert (
        client.get(
            c["base"] + f"/verification-runs/{sid}/analysis",
            headers=c["headers"]["owner"],
        ).status_code
        == 404
    )


def test_oversized_case_and_external_image_report_partial_coverage(
    context, monkeypatch
):
    c = context
    raw = b"9 " * 2000
    object_storage.write_bytes(c["keys"]["input"], raw)
    with c["sessions"]() as db:
        db.get(
            ProblemRow, c["pid"]
        ).statement += " ![그림](https://outside.invalid/secret.png)"
        case = db.scalar(select(Case))
        case.input_sha256 = hashlib.sha256(raw).hexdigest()
        db.commit()
    monkeypatch.setattr(settings, "verification_ai_file_max_bytes", 1024)
    sid = submit(c)
    request_completed(c)
    ai.process_one()
    coverage = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]["coverage"]
    assert coverage["partial"] and coverage["partial_testcases"] == 1
    assert any("외부 이미지" in note for note in coverage["notes"])
    evidence = json.loads(c["calls"][0][1][0]["text"])
    assert len(evidence["testcases"][0]["input"]) == 1024
    assert "앞부분" in evidence["testcases"][0]["input_note"]


def test_worker_crash_expires_without_automatic_rebilling_and_retry_limit(context):
    c = context
    sid = submit(c)
    request_completed(c)
    with c["sessions"]() as db:
        row = db.scalar(select(ai.Analysis))
        row.status = "running"
        row.started_at = now_utc() - timedelta(minutes=11)
        row.attempts = 3
        row.claim_token = "expired"
        db.commit()
    assert not ai.process_one() and not c["calls"]
    result = ai.analysis_detail(c["cid"], c["pid"], sid)["analysis"]
    assert result["status"] == "failed"
    response = client.post(
        c["base"] + f"/verification-runs/{sid}/analysis", headers=c["headers"]["owner"]
    )
    assert response.status_code == 429


@pytest.mark.parametrize("verdict", [
    SubmissionStatus.ACCEPTED,
    SubmissionStatus.WRONG_ANSWER,
    SubmissionStatus.COMPILE_ERROR,
    SubmissionStatus.SYSTEM_ERROR,
])
def test_saved_verdict_is_shared_without_an_ai_request_or_api_key(context, monkeypatch, verdict):
    c = context
    monkeypatch.setattr(settings, "openai_api_key", None)
    sid = submit(c, expected_status=verdict)
    # Log payload and metrics belong to the persisted submission, not the browser.
    with c["sessions"]() as db:
        row = db.get(SubmissionRow, sid)
        row.compile_message = "compiler diagnostic\n"
        db.commit()
    path = c["base"] + "/verification-runs"
    with TestClient(app) as other_browser:
        first = client.get(path, headers=c["headers"]["owner"])
        second = other_browser.get(path, headers=c["headers"]["author"])
    assert first.status_code == second.status_code == 200
    data = first.json()["data"]
    assert data == second.json()["data"]
    assert data["available"] is False
    assert len(data["runs"]) == 1
    run = data["runs"][0]
    assert run["analysis"] is None
    saved = run["submission"]
    assert saved["submission_id"] == sid
    assert saved["status"] == verdict.value
    assert saved["submitted_by_name"] == "owner"
    assert saved["submitted_at"]
    assert saved["failed_testcase_order"] == (None if verdict == SubmissionStatus.ACCEPTED else 1)
    assert saved["runtime_ms"] == (None if verdict in {SubmissionStatus.COMPILE_ERROR, SubmissionStatus.SYSTEM_ERROR} else 10)
    assert saved["memory_kb"] == (None if verdict in {SubmissionStatus.COMPILE_ERROR, SubmissionStatus.SYSTEM_ERROR} else 1024)
    assert saved["compile_message"] == "compiler diagnostic\n"
    assert "[input]\n2 3\n[expected]\n5\n[actual]\n4" in saved["judge_message"]
    assert "source_code" not in saved
    assert not c["calls"]
    assert client.get(path).status_code == 401
    assert client.get(path, headers=c["headers"]["reviewer"]).status_code == 403


def test_latest_request_and_its_analysis_win_when_older_judgment_finishes_last(context):
    c = context
    old = submit(c, claim=False)
    new = submit(c, claim=False)
    jobs = store.claim_jobs(c["node"].judge_node_id, SECRET, 2)
    by_submission = {job["submission"]["submission_id"]: job for job in jobs}

    def finish(sid, verdict, message):
        job = by_submission[sid]
        store.report_judge_result(
            job["judge_job_id"], SECRET, job["lease_token"], verdict,
            None, message, None if verdict == SubmissionStatus.ACCEPTED else 1, 10, 1024,
            testcase_results=[{"index": 1, "final_status": verdict.value, "runtime_ms": 10, "memory_kb": 1024}],
        )

    path = c["base"] + "/verification-runs"
    waiting = client.get(path, headers=c["headers"]["author"]).json()["data"]["runs"]
    assert len(waiting) == 1 and waiting[0]["submission"]["submission_id"] == new
    finish(new, SubmissionStatus.WRONG_ANSWER, "new result")
    ai.request_analysis(c["cid"], c["pid"], new)
    assert ai.process_one()
    report = ai.analysis_detail(c["cid"], c["pid"], new)["analysis"]
    finish(old, SubmissionStatus.ACCEPTED, "older request finished later")
    runs = client.get(path, headers=c["headers"]["author"]).json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["submission"]["submission_id"] == new
    assert runs[0]["submission"]["judge_message"] == "new result"
    assert runs[0]["analysis"]["analysis_id"] == report["analysis_id"]
    other = client.get(path + f"/{new}/analysis", headers=c["headers"]["author"])
    assert other.json()["data"]["analysis"]["report"] == REPORT
    # A new, matching verdict must not inherit the preceding mismatch analysis.
    latest = submit(c, expected_status=SubmissionStatus.ACCEPTED)
    runs = client.get(path, headers=c["headers"]["author"]).json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["submission"]["submission_id"] == latest
    assert runs[0]["analysis"] is None
    assert len(c["calls"]) == 1


def test_analysis_request_audit_keeps_problem_filename_and_request_time_status(context):
    c = context
    sid = submit(c)
    path = c['base'] + f'/verification-runs/{sid}/analysis'
    response = client.post(path, headers=c['headers']['owner'])
    assert response.status_code == 200, response.text
    result = response.json()['data']
    logs = client.get(f"/api/operator/contests/{c['cid']}/audit-logs", headers=c['headers']['owner']).json()['data']
    entry = next(log for log in logs if log['path'] == path and log['method'] == 'POST')['details']
    assert entry['target'] == {'problem_title': '두 수의 합', 'problem_code': 'A', 'original_filename': 'main.py'}
    assert entry['analysis'] == {'analysis_id': result['analysis']['analysis_id'], 'status': 'queued'}
    assert 'source_code' not in entry['target']
    assert 'report' not in entry['analysis']
