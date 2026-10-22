"""Bounded Editor tool queue with administrator-owned routing and node leases."""
import hashlib
import hmac
import json
import threading
import time
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import func, select
from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import EditorExecutionTaskRow as Task, EditorJudgeRunRow, JudgeJobRow, JudgeNodeRow
from app.services import editor_judging
from app.services.editor_protocol import PROTOCOL, MAX_WIRE_BYTES, unpack, validate_result
from app.services.errors import AppError
from app.services.node_credentials import valid_node_credential
from app.services.storage import object_storage
from app.settings import settings

ACTIVE = {"uploading", "waiting", "running"}
_gc_lock = threading.Lock()
_last_gc = 0.0


def available_slots(db, node):
    actual = sum(db.scalar(select(func.count()).select_from(model).where(model.assigned_node_id == node.judge_node_id, model.status.in_(statuses))) or 0
                 for model, statuses in [(JudgeJobRow, ["running"]), (EditorJudgeRunRow, ["preparing", "judging"]), (Task, ["running"])])
    return max(0, node.total_slots - actual)


def stats(session_factory=None):
    with (session_factory or SessionLocal)() as db:
        running, waiting = {}, 0
        for model, active in [(EditorJudgeRunRow, ["preparing", "judging"]), (Task, ["running"])]:
            for node, count in db.execute(select(model.assigned_node_id, func.count()).where(model.status.in_(active)).group_by(model.assigned_node_id)):
                if node:
                    running[node] = running.get(node, 0) + count
            waiting += db.scalar(select(func.count()).select_from(model).where(model.status == "waiting")) or 0
        return {"running_by_node": running, "pending_count": waiting, "running_count": sum(running.values())}


def capabilities():
    with SessionLocal() as db:
        nodes = db.scalars(select(JudgeNodeRow).where(JudgeNodeRow.schedulable.is_(True), JudgeNodeRow.allow_editor.is_(True))).all()
        ready = [n for n in nodes if PROTOCOL in (n.execution_capabilities or []) and n.execution_revision and editor_judging.aware(n.last_heartbeat_at) >= now_utc() - timedelta(seconds=settings.judge_node_active_window_seconds)]
        revision = hashlib.sha256(json.dumps(sorted((n.agent_version, n.execution_revision or "") for n in ready)).encode()).hexdigest()[:20]
        return {"protocol": PROTOCOL, "online_nodes": len(ready), "runtime_revision": "editor-agent-" + revision,
                "job_kinds": ["prepare", "verify", "validator-tests", "checker-tests", "stress", "package", "render"]}


def public(row, include_result=False):
    result = None
    if include_result and row.status == "completed" and row.result_key:
        result = json.loads(object_storage.read_bytes(row.result_key))
    return {"id": row.task_id, "status": row.status, "node_name": row.node_name,
            "agent_version": row.agent_version, "execution_revision": row.payload.get("execution_revision"), "error": row.error, "result": result}


def create(email, content, fingerprint):
    if hashlib.sha256(content).hexdigest() != fingerprint:
        raise AppError(401, "editor_content_mismatch", "실행 자료의 무결성 검사가 실패했습니다.")
    try:
        payload = unpack(content)
    except Exception as error:
        # Do not expose archive contents in errors.
        raise AppError(422, "editor_execution_invalid", "실행 자료의 형식·용량·무결성을 확인하세요.") from error
    with SessionLocal() as db:
        account = editor_judging.owner(db, email, lock=True)
        operation = hashlib.sha256((account.account_id + ":" + payload["operation_id"]).encode()).hexdigest()
        previous = db.scalar(select(Task).where(Task.operation_key == operation))
        if previous:
            if previous.status == "cancelled" and not previous.fingerprint:
                return public(previous)
            if previous.fingerprint != fingerprint:
                raise AppError(409, "editor_execution_changed", "같은 실행 요청의 자료가 변경되었습니다.")
            if previous.status != "uploading":
                return public(previous)
        personal = db.scalar(select(func.count()).select_from(Task).where(Task.owner_id == account.account_id, Task.status.in_(ACTIVE)))
        global_count = db.scalar(select(func.count()).select_from(Task).where(Task.status.in_(ACTIVE)))
        if not previous and (personal >= 20 or global_count >= 200):
            raise AppError(429, "editor_execution_busy", "실행 작업이 많습니다. 완료 후 다시 요청하세요.")
        task_id = operation[:32]
        key = "editor-execution/" + account.account_id + "/" + task_id + "/input.zip"
        row = previous
        if not row:
            row = Task(task_id=task_id, operation_key=operation, owner_id=account.account_id, fingerprint=fingerprint, status="uploading",
                   input_key=key, payload={"config": payload["config"], "runtime": payload["runtime"], "job_kind": payload["job_kind"], "input_sha256": fingerprint, "requires_zoj_runtime": True})
            db.add(row)
        db.commit()

    # A durable, non-runnable reservation protects cancellation and quota
    # checks while object storage operates without an account lock or DB pool.
    try:
        object_storage.write_bytes(key, content)
    except Exception as error:
        raise AppError(503, "editor_execution_storage_busy", "실행 자료 저장을 완료하지 못했습니다. 같은 요청으로 다시 시도하세요.") from error
    with SessionLocal() as db:
        row = db.scalar(select(Task).where(Task.task_id == task_id).with_for_update())
        if row.status == "uploading":
            row.status = "waiting"
            db.commit()
        return public(row)


def get(email, task_id, cancel=False):
    with SessionLocal() as db:
        account = editor_judging.owner(db, email)
        query = select(Task).where(Task.task_id == task_id, Task.owner_id == account.account_id)
        row = db.scalar(query.with_for_update() if cancel else query)
        if not row:
            raise AppError(404, "editor_execution_missing", "내 실행 요청을 찾을 수 없습니다.")
        if cancel and row.status in ACTIVE:
            row.status, row.lease_token, row.completed_at = "cancelled", None, now_utc()
            db.commit()
        return public(row, include_result=not cancel)


def recover(db):
    cutoff = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds)
    for row in db.scalars(select(Task).where(Task.status == "running", (Task.leased_at < cutoff) | Task.leased_at.is_(None)).with_for_update(skip_locked=True)):
        row.status = "waiting" if row.attempts < 3 else "failed"
        row.assigned_node_id = row.lease_token = row.leased_at = None
        if row.status == "failed":
            row.error, row.completed_at = "채점기 연결이 반복해서 중단되었습니다.", now_utc()


def maintenance(session_factory=None):
    global _last_gc
    if time.monotonic() - _last_gc < 60 or not _gc_lock.acquire(blocking=False):
        return
    try:
        with (session_factory or SessionLocal)() as db:
            for row in db.scalars(select(Task).where(Task.status.in_(ACTIVE), Task.created_at < now_utc() - timedelta(hours=1)).with_for_update(skip_locked=True)):
                row.status, row.lease_token, row.completed_at, row.error = "failed", None, now_utc(), "실행 대기·처리 한도를 초과했습니다."
            db.commit()
            for row in db.scalars(select(Task).where(Task.status.not_in(ACTIVE), Task.completed_at < now_utc() - timedelta(hours=48)).limit(1000).with_for_update(skip_locked=True)):
                for key in [row.input_key, row.result_key]:
                    if key:
                        object_storage.delete(key)
                db.delete(row)
            db.commit()
        _last_gc = time.monotonic()
    finally:
        _gc_lock.release()


def claim_jobs(node_id, node_secret, max_count, session_factory=None):
    maintenance(session_factory)
    with (session_factory or SessionLocal)() as db:
        node = db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.judge_node_id == node_id).with_for_update())
        if not valid_node_credential(node, node_secret):
            raise ValueError("node secret invalid")
        recover(db)
        db.flush()
        capacity = min(max_count, settings.judge_claim_max_batch_size, available_slots(db, node))
        if not node.allow_editor or PROTOCOL not in (node.execution_capabilities or []) or not node.execution_revision or capacity <= 0:
            db.commit()
            return []
        query = select(Task).where(Task.status == "waiting")
        if "editor-zoj-runtime-v1" not in (node.execution_capabilities or []):
            query = query.where(Task.payload["requires_zoj_runtime"].as_boolean().is_not(True))
        if "editor-icpc-languages-v1" not in (node.execution_capabilities or []):
            # New compiler and interpreter paths require an updated runtime image.
            query = query.where(Task.payload["requires_icpc"].as_boolean().is_not(True))
        rows = db.scalars(query.order_by(Task.created_at, Task.task_id).limit(capacity).with_for_update(skip_locked=True)).all()
        result = []
        for row in rows:
            row.status, row.assigned_node_id, row.lease_token, row.leased_at = "running", node_id, str(uuid4()), now_utc()
            row.node_name, row.agent_version = node.node_name, node.agent_version
            row.attempts += 1
            row.payload = {**row.payload, "execution_revision": node.execution_revision}
            result.append({"task_type": PROTOCOL, "judge_job_id": row.task_id, "lease_token": row.lease_token,
                           "submission": {"submission_id": row.task_id, "submission_kind": "editor_execution"},
                           "input_url": object_storage.internal_presigned_get_url(row.input_key),
                           "input_sha256": row.payload["input_sha256"], "runtime": row.payload["runtime"]})
        db.commit()
        return result


def update(task_id, node_secret, lease_token, *, result=None, error=None, session_factory=None):
    with (session_factory or SessionLocal)() as db:
        row = db.scalar(select(Task).where(Task.task_id == task_id).with_for_update())
        if not row:
            return None
        node = db.get(JudgeNodeRow, row.assigned_node_id) if row.assigned_node_id else None
        if not valid_node_credential(node, node_secret):
            raise ValueError("node secret invalid")
        if not row.lease_token or not hmac.compare_digest(row.lease_token.encode(), lease_token.encode()):
            raise ValueError("lease mismatch")
        if row.status in {"completed", "failed"}:
            if result is None and error is None:
                return {"accepted": True}
            if result is not None and row.result_key and json.loads(object_storage.read_bytes(row.result_key)) == result or error is not None and row.error == error:
                return {"accepted": True}
            raise ValueError("lease mismatch")
        if row.status != "running" or not row.leased_at or editor_judging.aware(row.leased_at) <= now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds):
            raise ValueError("lease mismatch")
        row.leased_at = now_utc()
        if result is not None:
            try:
                validate_result(result, row.payload["config"])
            except (ValueError, KeyError, TypeError) as exc:
                raise AppError(422, "editor_result_invalid", "실행 결과의 형식·산출물을 확인하세요.") from exc
            key = "editor-execution/" + row.owner_id + "/" + row.task_id + "/result.json"
            object_storage.write_bytes(key, json.dumps(result, allow_nan=False, separators=(",", ":")).encode())
            row.status, row.result_key, row.completed_at = "completed", key, now_utc()
        elif error is not None:
            row.status, row.error, row.completed_at = "failed", error[:4000], now_utc()
        db.commit()
        return {"accepted": True}



def cancel_operation(email, operation_id):
    """A cancellation tombstone also stops a late or lost create response."""
    with SessionLocal() as db:
        account = editor_judging.owner(db, email, lock=True)
        key = hashlib.sha256((account.account_id + ":" + operation_id).encode()).hexdigest()
        row = db.scalar(select(Task).where(Task.operation_key == key).with_for_update())
        if not row:
            row = Task(task_id=key[:32], operation_key=key, owner_id=account.account_id,
                       fingerprint="", payload={}, input_key="", status="cancelled", completed_at=now_utc())
            db.add(row)
        elif row.status in ACTIVE:
            row.status, row.lease_token, row.completed_at = "cancelled", None, now_utc()
        db.commit()
        return public(row)
