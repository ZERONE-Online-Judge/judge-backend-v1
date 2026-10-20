"""Full-suite evidence for registered operator verification code, never contestants."""
import base64
import hashlib

from app.orm_models import VerificationExecutionRow as Execution
from app.services import editor_judging
from app.services.errors import not_found
from app.services.storage import object_storage

CAPABILITY = "zoj-judge-full-v1"
MAX_PREVIEW = 256 * 1024


def attach(db, submission):
    row = Execution(submission_id=submission.submission_id, problem_id=submission.problem_id,
                    testcases=[], results=[], storage_keys=[], full_suite_complete=False)
    refresh_summary(row)
    db.add(row)


def claim(db, submission, node, job, active_set, cases):
    row = db.get(Execution, submission.submission_id)
    if row is None:
        return False
    row.testcases = [{key: getattr(case, key) for key in (
        "display_order", "input_storage_key", "output_storage_key", "input_sha256", "output_sha256"
    )} for case in cases]
    row.results = []
    row.full_suite_complete = False
    row.testcase_version = active_set.version if active_set else None
    row.node_name = node.node_name
    row.agent_version = node.agent_version
    row.lease_token = job.lease_token
    refresh_summary(row)
    return True



def requeue(db, submission_id):
    row = db.get(Execution, submission_id)
    if row is not None:
        row.results = []
        row.testcases = []
        row.full_suite_complete = False
        row.node_name = row.agent_version = row.lease_token = None
        refresh_summary(row)


def output_key(row, index, sha):
    return f"problems/{row.problem_id}/verification-outputs/{row.submission_id}/{index}-{sha}.out"


def merge(row, values, terminal=None):
    expected = {case["display_order"] for case in row.testcases}
    previous = {case["index"]: case for case in row.results}
    merged, incoming, writes = dict(previous), set(), {}
    for item in values:
        value = editor_judging.CaseResult.model_validate(item).model_dump()
        index = value["index"]
        if index not in expected or index in incoming:
            raise ValueError("lease mismatch: invalid testcase result")
        incoming.add(index)
        encoded = value.pop("output_base64")
        value["output_available"] = encoded is not None
        if encoded is not None:
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise ValueError("lease mismatch: invalid output encoding") from error
            original = value["output_total_bytes"]
            if len(content) > editor_judging.MAX_CASE_OUTPUT_BYTES or original is None or original < len(content) or value["output_truncated"] != (len(content) < original):
                raise ValueError("lease mismatch: invalid output size")
            sha = hashlib.sha256(content).hexdigest()
            value.update(output_size=len(content), output_sha256=sha)
            key = output_key(row, index, sha)
            if key not in row.storage_keys:
                writes[key] = content
        if index in previous and previous[index] != value:
            raise ValueError("lease mismatch: testcase result changed")
        merged[index] = value
    if sum(case.get("output_size", 0) for case in merged.values()) > editor_judging.MAX_RUN_OUTPUT_BYTES:
        raise ValueError("lease mismatch: output budget exceeded")
    complete = bool(expected) and set(merged) == expected
    if terminal is not None:
        status, failed = terminal
        if failed is not None and failed not in expected:
            raise ValueError("lease mismatch: unknown failed testcase")
        if not complete and status not in {"compile_error", "system_error"}:
            raise ValueError("lease mismatch: incomplete full-suite result")
        if complete:
            first = next((case for _, case in sorted(merged.items()) if case["final_status"] != "accepted"), None)
            if status != (first["final_status"] if first else "accepted") or failed != (first["index"] if first else None):
                raise ValueError("lease mismatch: aggregate differs from testcase results")
    for key, content in writes.items():
        object_storage.write_bytes(key, content, "application/octet-stream")
    row.storage_keys = list(dict.fromkeys([*row.storage_keys, *writes]))
    row.results = [value for _, value in sorted(merged.items())]
    row.full_suite_complete = complete if terminal is not None else False
    refresh_summary(row)
    return len(row.results)


def summary(row):
    return row.summary if row is not None else None


def refresh_summary(row):
    row.summary = {"collect_all_results": True, "total": len(row.testcases), "completed": len(row.results),
            "full_suite_complete": row.full_suite_complete, "node_name": row.node_name,
            "agent_version": row.agent_version, "testcase_version": row.testcase_version,
            "observed_statuses": sorted({case["final_status"] for case in row.results}),
            "runtime_ms": max((case["runtime_ms"] for case in row.results if case["runtime_ms"] is not None), default=None),
            "memory_kb": max((case["memory_kb"] for case in row.results if case["memory_kb"] is not None), default=None)}


def expectation_matches(expected, actual, row=None):
    if row is None:
        return expected == actual
    statuses = {case["final_status"] for case in row.results}
    return row.full_suite_complete and "system_error" not in statuses and (
        statuses == {"accepted"} if expected == "accepted" else expected in statuses)


def public_case(value):
    return {key: value.get(key) for key in ("index", "final_status", "runtime_ms", "memory_kb", "output_available", "output_truncated")}


def case_list(cid, pid, sid):
    from app.services.verification_ai import SessionLocal, _find_run
    with SessionLocal() as db:
        _find_run(db, cid, pid, sid)
        row = db.get(Execution, sid)
        return {"execution": summary(row), "indices": [case["display_order"] for case in row.testcases] if row else [], "testcases": [public_case(value) for value in row.results] if row else []}


def preview(key, sha=None):
    with object_storage.open_reader(key) as source:
        content = source.read(MAX_PREVIEW + 1)
    size = object_storage.size_bytes(key)
    truncated = len(content) > MAX_PREVIEW
    content = content[:MAX_PREVIEW]
    if sha and not truncated and hashlib.sha256(content).hexdigest() != sha:
        raise not_found("저장된 채점 자료의 무결성을 확인하지 못했습니다.")
    return {"text": content.decode("utf-8", errors="replace"), "size": size, "truncated": truncated}


def case_detail(cid, pid, sid, index):
    from app.services.verification_ai import SessionLocal, _find_run
    with SessionLocal() as db:
        _find_run(db, cid, pid, sid)
        row = db.get(Execution, sid)
        case = next((case for case in row.testcases if case["display_order"] == index), None) if row else None
        result = next((value for value in row.results if value["index"] == index), None) if row else None
        if not case or not result:
            raise not_found("아직 저장된 테스트 결과가 없습니다.")
        actual = None
        if result["output_available"]:
            actual = preview(output_key(row, index, result["output_sha256"]), result["output_sha256"])
            actual.update(size=result["output_total_bytes"], truncated=actual["truncated"] or result["output_truncated"])
        return {**public_case(result), "judge_message": result["judge_message"],
                "input": preview(case["input_storage_key"], case["input_sha256"]),
                "expected": preview(case["output_storage_key"], case["output_sha256"]), "actual": actual}
