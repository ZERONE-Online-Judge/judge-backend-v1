"""Problem-scoped requests, continuations, cancellation and shared evidence."""

import base64
import copy
import io
import zipfile
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from app.models import now_utc
from app.orm_models import (
    VerificationTaskRow as Task,
    VerificationAnalysisRow as Analysis,
    VerificationSnapshotRow as Snapshot,
)
from app.services import verification_ai as ai
from app.services import verification_agent_tools as caps
from app.services import verification_workspace as workspace
from app.services.errors import AppError, not_found
from app.settings import settings

ACTIVE = {"queued", "running"}


def available():
    return ai.enabled() and settings.verification_agent_enabled


def find(db, cid, pid, tid):
    task, row = db.get(Task, tid), db.get(Analysis, tid)
    if not task or not row or task.contest_id != cid or task.problem_id != pid:
        raise not_found()
    return task, row


def data(db, task, row, *, full=False):
    return {
        "task_id": task.analysis_id,
        "parent_task_id": task.parent_task_id,
        "goal": task.goal,
        "source_asset_id": task.source_asset_id,
        "cancel_requested": task.cancel_requested,
        "analysis": ai._analysis_data(row, full=full, db=db),
    }


def list_tasks(cid, pid):
    from app.services.verification_agent import limits

    with ai.SessionLocal() as db:
        problem = ai._check_problem(db, cid, pid)
        refs = caps.references(db, SimpleNamespace(contest_id=cid, problem_id=pid))
        rows = db.execute(
            select(Task, Analysis)
            .join(Analysis, Analysis.analysis_id == Task.analysis_id)
            .where(Task.contest_id == cid, Task.problem_id == pid)
            .order_by(Task.created_at.desc(), Task.analysis_id.desc())
            .limit(40)
        ).all()
        return {
            "available": available(),
            "limits": {**limits(), "concurrency": ai.concurrency()},
            "tasks": [data(db, task, row) for task, row in rows],
            "sources": [
                {"asset_id": r["asset_id"], "filename": r["filename"]} for r in refs
            ],
            "context_hash": ai.digest(ai.current_context(db, problem)),
        }


def detail(cid, pid, tid):
    with ai.SessionLocal() as db:
        problem = ai._check_problem(db, cid, pid)
        task, row = find(db, cid, pid, tid)
        result = data(db, task, row, full=True)
        result["stale"] = row.context_hash != ai.digest(ai.current_context(db, problem))
        return result


def create(cid, pid, goal, source_asset_id=None, parent_task_id=None, created_by=None):
    from app.services import verification_agent as agent

    goal = goal.strip()
    if not 5 <= len(goal) <= 4000:
        raise AppError(
            422, "verification_goal_invalid", "검증 요청은 5~4,000자로 입력해 주세요."
        )
    if not available():
        raise AppError(
            503,
            "verification_ai_not_configured",
            "검증 에이전트의 서버 연결을 확인해 주세요.",
        )
    with ai.SessionLocal() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(select(func.pg_advisory_xact_lock(74327921)))
        problem = ai._check_problem(db, cid, pid)
        context = ai.current_context(db, problem)
        context_hash = ai.digest(context)
        refs = caps.references(db, SimpleNamespace(contest_id=cid, problem_id=pid))
        parent = None
        if parent_task_id:
            previous, parent = find(db, cid, pid, parent_task_id)
            if parent.status in ACTIVE or parent.claim_token:
                raise AppError(
                    409,
                    "verification_task_running",
                    "진행 중인 작업을 마치거나 중지한 뒤 이어서 요청해 주세요.",
                )
            if (
                parent.context_hash != context_hash
                or parent.evidence.get("references") != refs
            ):
                raise AppError(
                    409,
                    "verification_context_changed",
                    "문제 자료가 변경됐습니다. 최신 자료로 새 작업을 시작해 주세요.",
                )
            source_asset_id = previous.source_asset_id
        evidence = {
            "mode": "task",
            "goal": goal,
            "references": refs,
            "source_code": "",
            "language": "python311",
            "actual_status": None,
            "expected_status": None,
            "compile_message": None,
            "judge_message": None,
        }
        if source_asset_id:
            files = caps.make_manifest(context, evidence, refs)
            file_id = "asset:" + source_asset_id
            if file_id not in files or files[file_id]["category"] != "reference":
                raise not_found("현재 문제에 등록된 검증 코드를 선택해 주세요.")
            try:
                source, language = caps.artifact(
                    {"artifacts": {}}, files, file_id, "python311"
                )
            except (caps.ToolError, UnicodeError) as error:
                raise AppError(
                    422,
                    "verification_source_invalid",
                    (
                        str(error)
                        if isinstance(error, caps.ToolError)
                        else "UTF-8 코드만 지원합니다."
                    ),
                ) from None
            evidence.update(source_code=source, language=language)
        key = ai.digest(
            {
                "task": evidence,
                "context": context_hash,
                "parent": parent_task_id,
                "model": settings.verification_agent_model,
                "version": agent.PROMPT_VERSION,
            }
        )
        cached = db.scalar(select(Analysis).where(Analysis.cache_key == key))
        if cached and db.get(Task, cached.analysis_id):
            return data(db, db.get(Task, cached.analysis_id), cached, full=True)
        pending = (
            db.scalar(
                select(func.count())
                .select_from(Analysis)
                .where(Analysis.status.in_(ACTIVE))
            )
            or 0
        )
        if pending >= 20:
            raise AppError(
                429,
                "verification_queue_full",
                "검증 대기 작업이 많습니다. 잠시 후 다시 요청해 주세요.",
            )
        ai.insert_once(
            db,
            Snapshot,
            dict(
                context_hash=context_hash,
                contest_id=cid,
                problem_id=pid,
                context=context,
                created_at=now_utc(),
            ),
            "context_hash",
        )
        row = Analysis(
            analysis_id=str(uuid4()),
            contest_id=cid,
            problem_id=pid,
            cache_key=key,
            status="queued",
            model=settings.verification_agent_model,
            engine_version=2,
            context_hash=context_hash,
            evidence=evidence,
            attempts=0,
            requested_at=now_utc(),
        )
        db.add(row)
        task = Task(
            analysis_id=row.analysis_id,
            contest_id=cid,
            problem_id=pid,
            goal=goal,
            source_asset_id=source_asset_id,
            parent_task_id=parent_task_id,
            created_by=created_by,
        )
        db.add(task)
        state = agent.initial_state(db, row, context)
        if parent:
            inherit(state, parent)
        row.agent_state = state
        db.commit()
        db.refresh(task)
        db.refresh(row)
        return data(db, task, row, full=True)


def inherit(state, parent):
    """Carry artifacts and concise evidence, never replay an entire old transcript."""
    old = parent.agent_state or {}
    state["workspace"] = copy.deepcopy(old.get("workspace", {}))
    state["workspace_executables"] = list(old.get("workspace_executables", []))
    state["workspace_readonly"] = copy.deepcopy(old.get("workspace_readonly", {}))
    omitted = []
    for key, item in old.get("artifacts", {}).items():
        extension = {
            "python313": "py", "python311": "py",
            "cpp17": "cpp",
            "c99": "c",
            "java8": "java", "pypy3": "py", "kotlin": "kt",
        }.get(item["language"], "txt")
        path = f"previous/{parent.analysis_id}/{key}.{extension}"
        try:
            workspace.put(state, path, item["source"].encode())
        except caps.ToolError:
            omitted.append(key)
    import json

    handoff = {
        "previous_task_id": parent.analysis_id,
        "previous_goal": parent.evidence.get("goal"),
        "previous_summary": (parent.report or {}).get("summary", "")[:3000],
        "previous_question": old.get("question"),
        "findings": old.get("findings", [])[-8:],
        "workspace_files": workspace.manifest(state),
        "omitted_candidates": omitted,
        "next": "이어받은 결론은 과거 근거다. 필요한 파일과 실행 결과를 다시 확인하고 이번 요청을 해결하라.",
    }
    state["history"].append(
        {"role": "user", "content": json.dumps(handoff, ensure_ascii=False)}
    )
    state["parent_task_id"] = parent.analysis_id


def cancel(cid, pid, tid):
    from app.services.verification_agent import cancel_pending_trials

    with ai.SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.analysis_id == tid).with_for_update())
        task, row = find(db, cid, pid, tid)
        if row.status in ACTIVE or row.status == "awaiting_input":
            task.cancel_requested = True
            cancel_pending_trials(db, tid)
            if not row.claim_token:
                row.status, row.completed_at = "stopped", now_utc()
                state = copy.deepcopy(row.agent_state or {})
                state.update(phase="사용자가 중지함", pending=[])
                state.pop("history", None)
                row.agent_state = state
            db.commit()
        return data(db, task, row, full=True)


def stop_requested(tid):
    with ai.SessionLocal() as db:
        return bool(
            db.scalar(select(Task.cancel_requested).where(Task.analysis_id == tid))
        )


def archive(cid, pid, tid):
    with ai.SessionLocal() as db:
        _, row = find(db, cid, pid, tid)
        files = (row.agent_state or {}).get("workspace", {})
        if not files:
            raise not_found("저장된 작업 파일이 없습니다.")
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path, value in files.items():
                zipped.writestr(
                    workspace.path_name(path), base64.b64decode(value, validate=True)
                )
        return result.getvalue()
