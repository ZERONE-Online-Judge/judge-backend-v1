"""Capabilities of the verifier. No shell, arbitrary paths, URLs or verdict writes.

Only existing isolate judge workers execute proposed solutions. Problem resources
are immutable inputs; candidates are separate operator test submissions.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import func, select

from app.models import now_utc
from app.orm_models import (
    JudgeJobRow,
    JudgeNodeRow,
    ProblemAssetRow,
    ProblemRow,
    SubmissionRow,
    VerificationTrialRow as Trial,
)
from app.services import verification_ai as ai
from app.services import judge_performance
from app.services.storage import object_storage
from app.settings import settings

MAX_SOURCE = 128 * 1024
MAX_FILE = 32 * 1024 * 1024


class ToolError(ValueError):
    pass


def clean_log(value, limit=6000):
    return re.sub(
        r"^\[(?:input_storage_key|output_storage_key)\][^\n]*\n?",
        "",
        value or "",
        flags=re.M,
    )[:limit]


def make_manifest(context, evidence, references):
    files = {
        "problem": {
            "category": "document",
            "name": "문제 지문",
            "text": context["problem"]["statement"],
        },
        "editorial": {
            "category": "document",
            "name": "해설",
            "text": context["problem"].get("editorial") or "",
        },
        "original": {
            "category": "code",
            "name": "원본 검증 코드",
            "text": evidence["source_code"],
        },
        "diagnostics": {
            "category": "document",
            "name": "원래 채점 로그",
            "text": clean_log(evidence.get("compile_message"), 16000)
            + "\n"
            + clean_log(evidence.get("judge_message"), 16000),
        },
    }
    if evidence.get("mode") == "task" and not evidence.get("source_code"):
        files.pop("original")
    files["judge-performance"] = {
        "category": "document",
        "name": "채점 서버 사양·연산별 TLE 실측 원본 기록",
        "text": judge_performance.evidence_text(),
        "read_only": True,
    }
    for case in context["testcases"]:
        for kind in ("input", "output"):
            files[f"case:{case['display_order']}:{kind}"] = {
                "category": "testcase",
                "name": f"테스트 #{case['display_order']} {kind}",
                "storage_key": case[f"{kind}_storage_key"],
                "sha256": case[f"{kind}_sha256"],
            }
    for item in [*context["assets"], *references]:
        files["asset:" + item["asset_id"]] = {
            **item,
            "category": (
                "reference"
                if "/verification-solutions/" in item["storage_key"]
                else "resource"
            ),
            "name": item["filename"],
            "read_only": (
                PurePosixPath(item["filename"]).name.lower() == "testlib.h"
                or any(
                    f"/{role}/" in item["storage_key"]
                    for role in ("checker", "validator", "package-resource")
                )
            ),
            "role": next(
                (
                    role
                    for role in ("validator", "checker", "package-resource")
                    if f"/{role}/" in item["storage_key"]
                ),
                "reference",
            ),
            "expected_status": next(
                (
                    status
                    for status in ai.EXPECTED
                    if f"/verification-solutions/{status}/" in item["storage_key"]
                ),
                None,
            ),
        }
    return files


def references(db, row):
    return [
        dict(
            asset_id=a.asset_id,
            filename=a.original_filename,
            storage_key=a.storage_key,
            sha256=a.sha256,
            size=a.file_size,
            mime_type=a.mime_type,
        )
        for a in db.scalars(
            select(ProblemAssetRow)
            .where(
                ProblemAssetRow.problem_id == row.problem_id,
                ProblemAssetRow.contest_id == row.contest_id,
                ProblemAssetRow.storage_key.contains("/verification-solutions/"),
            )
            .order_by(ProblemAssetRow.asset_id)
        ).all()
    ]


def file_bytes(files, file_id):
    item = files.get(file_id)
    if item is None:
        raise ToolError(
            "이 검증 자료 목록에 없는 파일입니다. list_files에서 ID를 확인하세요."
        )
    if "text" in item:
        return item["text"].encode("utf-8")
    # Read a bounded whole object and verify its checksum before exposing any range.
    # Large datasets still run on the judge; they are not pasted into the model.
    try:
        with object_storage.open_reader(item["storage_key"]) as stream:
            data = stream.read(MAX_FILE + 1)
    except Exception:
        raise ToolError("채점 당시 파일을 찾을 수 없습니다.") from None
    if len(data) > MAX_FILE:
        raise ToolError(
            "32 MiB보다 큰 파일은 모델 조회에서 제외합니다. 실제 채점에는 사용할 수 있습니다."
        )
    if not item.get("sha256") or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ToolError(
            "채점 당시 체크섬과 달라 파일을 제외했습니다. 최신 자료로 다시 채점해야 합니다."
        )
    return data


def read_file(files, file_id, offset, length):
    if (
        not isinstance(offset, int)
        or offset < 0
        or not isinstance(length, int)
        or not 1 <= length <= 12000
    ):
        raise ToolError("offset은 0 이상, length는 1~12000 바이트여야 합니다.")
    raw = file_bytes(files, file_id)
    if b"\x00" in raw[:4096] or files[file_id].get("mime_type", "").startswith(
        "image/"
    ):
        raise ToolError(
            "이 파일은 텍스트가 아닙니다. 이미지에는 read_image를 사용하세요."
        )
    end = min(offset + length, len(raw))
    return dict(
        file_id=file_id,
        text=raw[offset:end].decode("utf-8", errors="replace"),
        offset=offset,
        next_offset=end if end < len(raw) else None,
        total_bytes=len(raw),
        complete=offset == 0 and end == len(raw),
    )


def search_file(files, file_id, query):
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise ToolError("검색어는 1~200자여야 합니다.")
    raw = file_bytes(files, file_id)
    needle = query.encode()
    matches, start = [], 0
    while len(matches) < 8:
        found = raw.find(needle, start)
        if found < 0:
            break
        matches.append(
            {
                "offset": found,
                "excerpt": raw[max(0, found - 120) : found + len(needle) + 240].decode(
                    "utf-8", errors="replace"
                ),
            }
        )
        start = found + len(needle)
    return {"file_id": file_id, "matches": matches, "match_limit": 8}


def artifact(state, files, artifact_id, language):
    if artifact_id == "original":
        return file_bytes(files, "original").decode(), language
    if artifact_id in state["artifacts"]:
        entry = state["artifacts"][artifact_id]
        return entry["source"], entry["language"]
    if files.get(artifact_id, {}).get("category") == "reference":
        name = files[artifact_id]["name"].lower()
        lang = next(
            (
                lang
                for suffix, lang in (
                    (".cpp", "cpp17"),
                    (".cc", "cpp17"),
                    (".py", "python311"),
                    (".java", "java8"), (".kt", "kotlin"),
                    (".c", "c99"),
                )
                if name.endswith(suffix)
            ),
            None,
        )
        if not lang:
            raise ToolError("이 참조 코드의 언어는 자동 실행을 지원하지 않습니다.")
        raw = file_bytes(files, artifact_id)
        if len(raw) > MAX_SOURCE:
            raise ToolError("실행 가능한 참조 코드 크기를 초과했습니다.")
        return raw.decode("utf-8-sig"), lang
    raise ToolError(
        "없는 코드 ID입니다. original 또는 생성한 candidate ID를 사용하세요."
    )


def edit_code(state, files, language, base_id, replacements):
    if len(state["artifacts"]) >= 4:
        raise ToolError("수정 후보는 최대 4개입니다.")
    code, lang = artifact(state, files, base_id, language)
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= 12:
        raise ToolError("치환은 1~12개로 지정하세요.")
    for item in replacements:
        old, new = item.get("old"), item.get("new")
        if (
            not isinstance(old, str)
            or not old
            or not isinstance(new, str)
            or code.count(old) != 1
        ):
            raise ToolError(
                "old 문자열이 코드에서 정확히 한 번 등장해야 합니다. 먼저 필요한 범위를 읽으세요."
            )
        code = code.replace(old, new, 1)
        if len(code.encode()) > MAX_SOURCE:
            raise ToolError("수정 코드는 128 KiB 이하만 지원합니다.")
    key = "candidate-" + str(len(state["artifacts"]) + 1)
    state["artifacts"][key] = {
        "source": code,
        "language": lang,
        "base": base_id,
        "sha256": hashlib.sha256(code.encode()).hexdigest(),
    }
    return {
        "artifact_id": key,
        "bytes": len(code.encode()),
        "sha256": state["artifacts"][key]["sha256"],
    }


def trial_result(db, trial):
    sub = db.get(SubmissionRow, trial.submission_id)
    job = db.scalar(
        select(JudgeJobRow).where(JudgeJobRow.submission_id == trial.submission_id)
    )
    node = (
        db.get(JudgeNodeRow, job.assigned_node_id)
        if job and job.assigned_node_id
        else None
    )
    return {
        "submission_id": trial.submission_id,
        "artifact_id": trial.artifact_id,
        "scope": (
            "probe"
            if trial.probe
            else "all" if trial.testcase_orders is None else "selected"
        ),
        "probe": trial.probe,
        "testcase_orders": trial.testcase_orders,
        "testcase_count": trial.testcase_count,
        "status": sub.status if sub else "system_error",
        "failed_testcase_order": sub.failed_testcase_order if sub else None,
        "progress_current": sub.progress_current if sub else None,
        "runtime_ms": sub.runtime_ms if sub else None,
        "memory_kb": sub.memory_kb if sub else None,
        "judge_message": clean_log(sub.judge_message) if sub else "실행 기록이 삭제됨",
        "compile_message": clean_log(sub.compile_message) if sub else "",
        "agent_version": node.agent_version if node else None,
        "context_hash": trial.context_hash,
        "source_sha256": (
            hashlib.sha256(sub.source_code.encode()).hexdigest() if sub else None
        ),
        "language": sub.language if sub else None,
    }


def results(db, analysis_id):
    return [
        trial_result(db, trial)
        for trial in db.scalars(
            select(Trial)
            .where(Trial.analysis_id == analysis_id)
            .order_by(Trial.created_at, Trial.submission_id)
        ).all()
    ]


def run_code(row, state, files, context, artifact_id, orders, probe=None):
    code, language = artifact(state, files, artifact_id, row.evidence["language"])
    if len(code.encode()) > MAX_SOURCE:
        raise ToolError("128 KiB보다 큰 코드는 추가 실행에서 제외합니다.")
    valid = {case["display_order"] for case in context["testcases"]}
    if not valid:
        raise ToolError("활성 테스트케이스가 없습니다.")
    if not isinstance(orders, list) or any(
        type(n) is not int or n not in valid for n in orders
    ):
        raise ToolError(
            "존재하는 테스트 번호 배열을 지정하세요. 빈 배열은 전체 실행입니다."
        )
    chosen = sorted(set(orders)) or None
    if probe is not None:
        if set(probe) != {"input", "expected_output"} or any(
            not isinstance(v, str) or len(v.encode()) > 8192 for v in probe.values()
        ):
            raise ToolError(
                "제안 테스트의 입력과 기대 출력은 각각 8 KiB 이하 문자열이어야 합니다."
            )
        probe = {
            **probe,
            "expected_output_source": "AI hypothesis",
            "validator_checked": False,
        }
    key = ai.digest([row.analysis_id, row.context_hash, code, language, chosen, probe])
    with ai.SessionLocal() as db:
        # Serialize the small scheduling transaction across worker replicas.
        if db.bind.dialect.name == "postgresql":
            db.execute(select(func.pg_advisory_xact_lock(74327922)))
        trial = db.scalar(select(Trial).where(Trial.cache_key == key))
        if trial:
            return trial_result(db, trial)
        used = db.scalar(
            select(func.count())
            .select_from(Trial)
            .where(Trial.analysis_id == row.analysis_id)
        )
        if used >= max(1, min(24, state["limits"]["max_runs"])):
            raise ToolError("추가 채점 횟수 한도에 도달했습니다.")
        active = db.scalar(
            select(func.count())
            .select_from(Trial)
            .join(SubmissionRow, Trial.submission_id == SubmissionRow.submission_id)
            .where(SubmissionRow.status.in_(ai.PENDING))
        )
        if active >= 2:
            return {"waiting_for_capacity": True}
        problem = db.get(ProblemRow, row.problem_id)
        if (
            not problem
            or problem.contest_id != row.contest_id
            or ai.digest(ai.current_context(db, problem)) != row.context_hash
        ):
            raise ToolError(
                "문제·테스트 자료가 변경되었습니다. 이전 버전으로 추가 실행하지 않습니다. 다시 채점하세요."
            )
        submission_id = str(uuid4())
        sub = SubmissionRow(
            submission_id=submission_id,
            contest_id=row.contest_id,
            division_id=problem.division_id,
            problem_id=row.problem_id,
            submission_kind="verification_trial",
            submitted_by_name="AI 검증",
            language=language,
            source_code=code,
            status="waiting",
        )
        db.add(sub)
        db.flush()
        db.add(
            JudgeJobRow(
                submission_id=submission_id,
                contest_id=row.contest_id,
                division_id=problem.division_id,
                status="pending",
                queue_position=(
                    db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0
                )
                + 1,
            )
        )
        trial = Trial(
            submission_id=submission_id,
            analysis_id=row.analysis_id,
            contest_id=row.contest_id,
            problem_id=row.problem_id,
            context_hash=row.context_hash,
            cache_key=key,
            artifact_id=artifact_id,
            testcase_orders=chosen,
            testcase_count=1 if probe else len(chosen) if chosen else len(valid),
            probe=probe,
        )
        db.add(trial)
        db.commit()
        return trial_result(db, trial)


def prepare_claim(db, submission, job, problem, cases):
    """Called before creating judge payload. Reject changed resources, filter cases."""
    trial = db.get(Trial, submission.submission_id)
    if trial is None:
        return cases, False, True
    if not problem or ai.digest(ai.current_context(db, problem)) != trial.context_hash:
        submission.status = "system_error"
        submission.status_updated_at = now_utc()
        submission.judge_message = (
            "AI 검증 중 문제 자료가 변경되어 실행을 취소했습니다."
        )
        job.status = "failed"
        job.lease_token = None
        job.leased_at = None
        return [], True, False
    if trial.probe:
        return [], True, True
    selected = [
        c
        for c in cases
        if trial.testcase_orders is None or c.display_order in trial.testcase_orders
    ]
    if len(selected) != trial.testcase_count:
        raise RuntimeError("verification trial testcase mismatch")
    return selected, trial.testcase_orders is not None, True


def probe_payload(db, submission_id):
    trial = db.get(Trial, submission_id)
    if not trial or not trial.probe:
        return None
    return [
        {
            "testcase_id": submission_id,
            "display_order": 1,
            "input_text": trial.probe["input"],
            "output_text": trial.probe["expected_output"],
            "input_storage_key": "",
            "output_storage_key": "",
            "time_limit_ms_override": None,
            "memory_limit_mb_override": None,
        }
    ]


def inspect_judge(context):
    overrides = [
        {
            k: c.get(k)
            for k in (
                "display_order",
                "time_limit_ms_override",
                "memory_limit_mb_override",
            )
        }
        for c in context["testcases"]
        if c.get("time_limit_ms_override") is not None
        or c.get("memory_limit_mb_override") is not None
    ]
    with ai.SessionLocal() as db:
        nodes = db.scalars(
            select(JudgeNodeRow).where(
                JudgeNodeRow.schedulable.is_(True),
                JudgeNodeRow.last_heartbeat_at
                > now_utc()
                - timedelta(seconds=settings.judge_node_active_window_seconds),
            )
        ).all()
        return {
            "time_limit_ms": context["problem"]["time_limit_ms"],
            "memory_limit_mb": context["problem"]["memory_limit_mb"],
            "language_resource_limits": context["problem"].get(
                "language_resource_limits"
            ),
            "testcase_count": len(context["testcases"]),
            "testcase_overrides": overrides[:100],
            "overrides_omitted": max(0, len(overrides) - 100),
            "testcase_policy": "기본 제한을 공유하는 테스트는 나열하지 않습니다. overrides는 별도 제한이 설정된 테스트만 포함합니다.",
            "active_agents": len(nodes),
            "active_submission_slots": sum(n.total_slots for n in nodes),
            "performance_reference": judge_performance.reference(),
            "performance_evidence_file": "judge-performance",
            "agent_versions": sorted({n.agent_version for n in nodes}),
            "policy": "기존 isolate 샌드박스 및 동일 checker 사용. 언어/케이스 재정의 우선. 기본 Java 시간*2+1000ms, 메모리*2+16MB, Python 시간*3+2000ms, 메모리*2+32MB. BOM/NBSP 정규화. checker 인수: 입력, 참가자 출력, 정답 출력. validator 소스는 조회 가능하나 추가 제출에서 별도 실행하지 않음.",
        }
