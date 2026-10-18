"""Authenticated Editor snapshots dispatched to the existing judge protocol."""
import gzip
import base64
import copy
import hashlib
import hmac
import json
import re
import stat
import threading
import time
import zipfile
from datetime import timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import JUDGE_FINAL_STATUSES, now_utc
from app.orm_models import EditorJudgeRunRow as Run, IdentityAccountRow, JudgeNodeRow
from app.services.errors import AppError
from app.services.node_credentials import valid_node_credential
from app.services.storage import object_storage
from app.settings import settings
from app.judge_languages import LANGUAGES, NEW_LANGUAGES, ICPC_CAPABILITY

ACTIVE = {"uploading", "waiting", "preparing", "judging"}
RUNNING = {"preparing", "judging"}
MAX_ARCHIVE = 512 * 1024**2
MAX_CONTENT = 512 * 1024**2
HASH = r"^[a-f0-9]{64}$"
FULL_VERIFICATION = "editor-judge-full-v1"
MAX_CASE_OUTPUT_BYTES = 256 * 1024
MAX_RUN_OUTPUT_BYTES = 16 * 1024 * 1024
_maintenance_lock = threading.Lock()
_last_maintenance = 0.0


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Strict):
    name: str = Field(min_length=1, max_length=150)
    language: Literal["cpp17", "python313", "c99", "java8", "pypy3", "kotlin"]
    sha256: str = Field(pattern=HASH)


class Case(Strict):
    index: int = Field(ge=1, le=1000000)
    input_sha: str = Field(pattern=HASH)
    answer_sha: str = Field(pattern=HASH)


class CaseResult(Strict):
    index: int = Field(ge=1, le=1000000)
    final_status: Literal["accepted", "wrong_answer", "time_limit_exceeded", "memory_limit_exceeded", "runtime_error", "output_limit_exceeded", "system_error"]
    runtime_ms: int | None = Field(default=None, ge=0)
    memory_kb: int | None = Field(default=None, ge=0)
    judge_message: str = Field(default="", max_length=2048)
    output_base64: str | None = Field(default=None, max_length=4 * ((MAX_CASE_OUTPUT_BYTES + 2) // 3))
    output_total_bytes: int | None = Field(default=None, ge=0)
    output_truncated: bool = False


class Resource(Strict):
    name: str = Field(min_length=1, max_length=150)
    role: Literal["checker", "package-resource"]
    sha256: str = Field(pattern=HASH)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if value in {".", ".."} or any(c in value for c in ("/", "\\")) or any(ord(c) < 32 for c in value):
            raise ValueError("Unsafe resource filename")
        return value


class Snapshot(Strict):
    format: Literal["zoj-editor-judge-v1"]
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    context_hash: str = Field(pattern=HASH)
    source: Source
    time_limit_ms: int = Field(ge=100, le=10000)
    memory_limit_mb: int = Field(ge=16, le=1024)
    collect_all_results: bool = False
    testcases: list[Case] = Field(min_length=1, max_length=1000)
    resources: list[Resource] = Field(min_length=1, max_length=100)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def owner(db, email, lock=False):
    query = select(IdentityAccountRow).where(IdentityAccountRow.email == email.lower())
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if not row:
        raise AppError(403, "editor_identity_missing", "ZOJ에 로그인한 뒤 검증하세요.")
    return row


def public_run(row):
    return {
        "id": row.run_id, "status": row.status,
        "progress_current": row.progress_current, "progress_total": row.testcase_count,
        "node_name": row.node_name, "agent_version": row.agent_version,
        "attempts": row.attempts,
        "result": row.result, "runtime_revision": row.payload["runtime_revision"],
    }


def capabilities():
    with SessionLocal() as db:
        nodes = db.scalars(select(JudgeNodeRow).where(JudgeNodeRow.schedulable.is_(True), JudgeNodeRow.allow_editor.is_(True))).all()
        versions = sorted({n.agent_version for n in nodes})
        revision = hashlib.sha256(json.dumps([FULL_VERIFICATION, settings.release_version, versions], separators=(",", ":")).encode()).hexdigest()[:16]
        return {
            "backend": "zoj", "runtime_revision": "zoj-judge-" + revision,
            "languages": list(LANGUAGES),
            "full_suite_protocol": FULL_VERIFICATION,
            "full_suite_online_nodes": sum(FULL_VERIFICATION in (n.execution_capabilities or []) and aware(n.last_heartbeat_at) >= now_utc() - timedelta(seconds=settings.judge_node_active_window_seconds) for n in nodes),
            "standard_io_only": True, "contest_submissions_first": True,
            "online_nodes": sum(aware(n.last_heartbeat_at) >= now_utc() - timedelta(seconds=settings.judge_node_active_window_seconds) for n in nodes),
        }


def create(email, source, fingerprint):
    """Validate every byte before storage; the signed caller supplies no URLs."""
    source.seek(0, 2)
    if source.tell() > MAX_ARCHIVE:
        raise AppError(413, "editor_judge_archive_large", "채점 자료 ZIP은 512MiB 이하여야 합니다.")
    source.seek(0)
    try:
        archive = zipfile.ZipFile(source)
        entries = archive.infolist()
        if len(entries) > 2102 or len({i.filename for i in entries}) != len(entries):
            raise ValueError("Duplicate or too many archive entries")
        if sum(i.file_size for i in entries) > MAX_CONTENT:
            raise ValueError("Expanded snapshot exceeds 512MiB")
        for entry in entries:
            mode = entry.external_attr >> 16
            if entry.flag_bits & 1 or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
                raise ValueError("Only unencrypted regular files are allowed")
            if entry.filename != "manifest.json" and not re.fullmatch(r"blobs/[a-f0-9]{64}", entry.filename):
                raise ValueError("Unsafe snapshot archive path")
        if archive.getinfo("manifest.json").file_size > 2 * 1024**2:
            raise ValueError("Snapshot manifest exceeds 2MiB")
        snapshot = Snapshot.model_validate_json(archive.read("manifest.json"))
        if len({c.index for c in snapshot.testcases}) != len(snapshot.testcases):
            raise ValueError("Duplicate testcase order")
        if sum(r.role == "checker" for r in snapshot.resources) != 1 or len({r.name for r in snapshot.resources}) != len(snapshot.resources):
            raise ValueError("Exactly one checker and unique resource filenames are required")
        hashes = {snapshot.source.sha256}
        hashes.update(s for c in snapshot.testcases for s in (c.input_sha, c.answer_sha))
        hashes.update(r.sha256 for r in snapshot.resources)
        if {i.filename for i in entries} != {"manifest.json", *("blobs/" + s for s in hashes)}:
            raise ValueError("Snapshot files do not match the manifest")
        content = {}
        for sha in hashes:
            if archive.getinfo("blobs/" + sha).file_size > 128 * 1024**2:
                raise ValueError("A snapshot file exceeds 128MiB")
            blob = archive.read("blobs/" + sha)
            if hashlib.sha256(blob).hexdigest() != sha:
                raise ValueError("Snapshot content hash mismatch")
            content[sha] = blob
        if len(content[snapshot.source.sha256]) > 1024**2:
            raise ValueError("Solution source exceeds 1MiB")
        code = content[snapshot.source.sha256].decode("utf-8-sig")
        cases = [
            {"display_order": c.index, "input_text": content[c.input_sha].decode("utf-8-sig"),
             "output_text": content[c.answer_sha].decode("utf-8-sig")}
            for c in sorted(snapshot.testcases, key=lambda c: c.index)
        ]
    except (ValueError, KeyError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValidationError) as error:
        raise AppError(422, "editor_judge_snapshot_invalid", "채점 자료 형식·파일 무결성을 확인하세요.") from error
    revision = capabilities()["runtime_revision"]
    with SessionLocal() as db:
        # Reserve the quota and dataset references before storage I/O. GC uses
        # the same owner lock, but uploads must never keep that lock or a DB
        # connection while writing potentially hundreds of shared objects.
        account = owner(db, email, lock=True)
        operation = hashlib.sha256((account.account_id + "\n" + snapshot.operation_id).encode()).hexdigest()
        existing = db.scalar(select(Run).where(Run.operation_key == operation))
        if existing:
            if existing.status == "cancelled" and not existing.fingerprint:
                return public_run(existing)
            if existing.fingerprint != fingerprint:
                raise AppError(409, "editor_judge_request_changed", "같은 채점 요청의 자료가 변경되었습니다.")
            if existing.status != "uploading":
                return public_run(existing)
        if not existing and db.scalar(select(func.count()).select_from(Run).where(Run.owner_id == account.account_id, Run.status.in_(ACTIVE))) >= 32:
            raise AppError(429, "editor_judge_busy", "내 계정의 채점 작업이 많습니다. 완료 후 다시 요청하세요.")
        if not existing and db.scalar(select(func.count()).select_from(Run).where(Run.status.in_(ACTIVE))) >= 256:
            raise AppError(429, "editor_judge_busy", "채점 대기열이 가득 찼습니다. 잠시 후 다시 요청하세요.")
        dataset_key = hashlib.sha256(json.dumps([
            snapshot.project_id, snapshot.context_hash, snapshot.time_limit_ms, snapshot.memory_limit_mb,
            [c.model_dump() for c in sorted(snapshot.testcases, key=lambda c: c.index)],
            [r.model_dump() for r in sorted(snapshot.resources, key=lambda r: r.name)],
        ], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prefix = "editor-judge/" + account.account_id + "/" + dataset_key + "/"
        bundle_key = prefix + "testcases.json.gz"
        case_refs = [{"display_order": c.index, "input_storage_key": prefix + c.input_sha,
                      "output_storage_key": prefix + c.answer_sha} for c in sorted(snapshot.testcases, key=lambda c: c.index)]
        resources = [{"original_filename": r.name, "role": r.role, "storage_key": prefix + r.sha256,
                      "sha256": r.sha256} for r in snapshot.resources]
        hashes = sorted({sha for c in snapshot.testcases for sha in (c.input_sha, c.answer_sha)} | {r.sha256 for r in snapshot.resources})
        written = [bundle_key, *(prefix + sha for sha in hashes)]
        row = existing
        if not row:
            row = Run(operation_key=operation, owner_id=account.account_id, dataset_key=dataset_key, project_id=snapshot.project_id,
                      fingerprint=fingerprint, testcase_count=len(cases), storage_keys=written, status="uploading",
                      payload={"language": snapshot.source.language, "source_code": code,
                               "source_name": snapshot.source.name, "context_hash": snapshot.context_hash,
                               "time_limit_ms": snapshot.time_limit_ms, "memory_limit_mb": snapshot.memory_limit_mb,
                               "collect_all_results": snapshot.collect_all_results,
                               "bundle_key": bundle_key, "base_bundle_key": bundle_key, "excluded_orders": [],
                               "testcases": case_refs,
                               "dataset_storage_keys": list(written),
                               "resources": resources, "runtime_revision": revision})
            db.add(row)
        db.commit()
        run_id = row.run_id

    try:
        if object_storage.size_bytes(bundle_key) is None:
            object_storage.write_bytes(bundle_key, gzip.compress(json.dumps({"testcases": cases}, ensure_ascii=False).encode(), mtime=0), "application/gzip")
        for sha in hashes:
            if object_storage.size_bytes(prefix + sha) is None:
                object_storage.write_bytes(prefix + sha, content[sha])
    except Exception as error:
        # Keep the reservation so an identical retry can finish the upload.
        # Deleting shared content here could break a concurrent solution run.
        # Abandoned reservations and their objects use the normal retention GC.
        raise AppError(503, "editor_judge_storage_busy", "채점 자료 저장을 완료하지 못했습니다. 같은 요청으로 다시 시도하세요.") from error

    with SessionLocal() as db:
        row = db.scalar(select(Run).where(Run.run_id == run_id).with_for_update())
        if row.status == "uploading":
            row.status = "waiting"
            db.commit()
        # A cancellation during upload must never become a runnable job.
        return public_run(row)


def cancel_operation(email, operation_id):
    """Cancel even when the upload response was lost before returning a run ID."""
    with SessionLocal() as db:
        account = owner(db, email, lock=True)
        operation = hashlib.sha256((account.account_id + "\n" + operation_id).encode()).hexdigest()
        row = db.scalar(select(Run).where(Run.operation_key == operation).with_for_update())
        if not row:
            row = Run(operation_key=operation, owner_id=account.account_id, project_id="0" * 32,
                      fingerprint="", testcase_count=0, storage_keys=[],
                      payload={"runtime_revision": "cancelled"}, status="cancelled", completed_at=now_utc())
            db.add(row)
        elif row.status in ACTIVE:
            row.status, row.lease_token, row.completed_at = "cancelled", None, now_utc()
        db.commit()
        return public_run(row)


def resume(email, parent_id, operation_id):
    """Remove the observed failure using stored immutable input, without upload."""
    with SessionLocal() as db:
        account = owner(db, email, lock=True)
        parent = db.scalar(select(Run).where(Run.run_id == parent_id, Run.owner_id == account.account_id).with_for_update())
        failed = (parent.result or {}).get("failed_testcase_order") if parent else None
        if not parent or parent.payload.get("collect_all_results") or parent.status in ACTIVE or not failed or parent.testcase_count < 2:
            raise AppError(409, "editor_judge_resume_invalid", "실패가 확인된 테스트의 나머지 자료만 재검증할 수 있습니다.")
        operation = hashlib.sha256((account.account_id + "\n" + operation_id).encode()).hexdigest()
        fingerprint = hashlib.sha256((parent_id + "\n" + str(failed)).encode()).hexdigest()
        existing = db.scalar(select(Run).where(Run.operation_key == operation))
        if existing:
            if existing.fingerprint != fingerprint:
                raise AppError(409, "editor_judge_request_changed", "같은 채점 요청의 자료가 변경되었습니다.")
            return public_run(existing)
        if db.scalar(select(func.count()).select_from(Run).where(Run.owner_id == account.account_id, Run.status.in_(ACTIVE))) >= 32:
            raise AppError(429, "editor_judge_busy", "내 계정의 채점 작업이 많습니다.")
        if db.scalar(select(func.count()).select_from(Run).where(Run.status.in_(ACTIVE))) >= 256:
            raise AppError(429, "editor_judge_busy", "채점 대기열이 가득 찼습니다.")
        payload = copy.deepcopy(parent.payload)
        excluded = set(payload["excluded_orders"]) | {failed}
        if object_storage.size_bytes(payload["base_bundle_key"]) is None:
            raise AppError(410, "editor_judge_snapshot_expired", "임시 채점 자료가 만료되었습니다. 새 검증을 요청하세요.")
        all_cases = json.loads(gzip.decompress(object_storage.read_bytes(payload["base_bundle_key"])))['testcases']
        remaining = [c for c in all_cases if c['display_order'] not in excluded]
        if len(remaining) != parent.testcase_count - 1:
            raise AppError(409, "editor_judge_snapshot_changed", "보관된 테스트 자료의 개수가 다릅니다.")
        key = "editor-judge/" + account.account_id + "/variants/" + operation + "-" + fingerprint + ".json.gz"
        object_storage.write_bytes(key, gzip.compress(json.dumps({"testcases": remaining}, ensure_ascii=False).encode(), mtime=0), "application/gzip")
        payload["bundle_key"], payload["excluded_orders"] = key, sorted(excluded)
        payload["testcases"] = [c for c in payload["testcases"] if c["display_order"] != failed]
        row = Run(operation_key=operation, owner_id=account.account_id, dataset_key=parent.dataset_key,
                  project_id=parent.project_id, fingerprint=fingerprint, testcase_count=len(remaining),
                  payload=payload, storage_keys=[key])
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(Run).where(Run.operation_key == operation))
            if existing and existing.fingerprint == fingerprint:
                return public_run(existing)
            object_storage.delete(key)
            raise AppError(409, "editor_judge_request_changed", "같은 채점 요청의 자료가 변경되었습니다.")
        return public_run(row)


def get_run(email, run_id, cancel=False):
    with SessionLocal() as db:
        account = owner(db, email)
        query = select(Run).where(Run.run_id == run_id, Run.owner_id == account.account_id)
        row = db.scalar(query.with_for_update() if cancel else query)
        if not row:
            raise AppError(404, "editor_judge_run_missing", "내 채점 요청을 찾을 수 없습니다.")
        if cancel and row.status in ACTIVE:
            row.status = "cancelled"
            row.lease_token = None
            row.completed_at = now_utc()
            db.commit()
        return public_run(row)


def maintenance(session_factory=None):
    global _last_maintenance
    if time.monotonic() - _last_maintenance < 60 or not _maintenance_lock.acquire(blocking=False):
        return
    try:
        with (session_factory or SessionLocal)() as db:
            for row in db.scalars(select(Run).where(Run.status.in_(ACTIVE), Run.created_at < now_utc() - timedelta(hours=1)).with_for_update(skip_locked=True)):
                row.status = "system_error"
                row.result = {"final_status": "system_error", "judge_message": "채점 대기·실행 한도를 초과했습니다."}
                row.lease_token = None
                row.completed_at = now_utc()
            db.commit()
            old_ids = db.scalars(select(Run.run_id).where(Run.status.not_in(ACTIVE), Run.created_at < now_utc() - timedelta(hours=48)).limit(1000)).all()
            for run_id in old_ids:
                candidate = db.get(Run, run_id)
                if not candidate:
                    continue
                # Creation and subset requests acquire owner then run locks.
                # Use the same order so GC cannot race new dataset references.
                db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.account_id == candidate.owner_id).with_for_update())
                row = db.scalar(select(Run).where(Run.run_id == run_id).with_for_update())
                if not row or row.status in ACTIVE or aware(row.created_at) >= now_utc() - timedelta(hours=48):
                    db.rollback()
                    continue
                # Keep the row on storage failure so a later sweep can retry.
                shared = db.scalar(select(Run.run_id).where(Run.owner_id == row.owner_id, Run.dataset_key == row.dataset_key, Run.run_id != row.run_id).limit(1))
                keys = set(row.storage_keys)
                if not shared:
                    keys.update(row.payload.get("dataset_storage_keys", []))
                    if row.payload.get("base_bundle_key"):
                        keys.add(row.payload["base_bundle_key"])
                    keys.update(r["storage_key"] for r in row.payload.get("resources", []))
                    keys.update(c[k] for c in row.payload.get("testcases", []) for k in ("input_storage_key", "output_storage_key"))
                for key in keys:
                    if "/variants/" in key or "/outputs/" in key or not shared:
                        object_storage.delete(key)
                db.delete(row)
                db.commit()
            db.commit()
        _last_maintenance = time.monotonic()
    finally:
        _maintenance_lock.release()


def claim_jobs(node_id, node_secret, max_count, session_factory=None):
    if max_count <= 0:
        return []
    with (session_factory or SessionLocal)() as db:
        node = db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.judge_node_id == node_id).with_for_update())
        if not valid_node_credential(node, node_secret):
            raise ValueError("node secret invalid")
        cutoff = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds)
        expired = db.scalars(select(Run).where(Run.status.in_(RUNNING), Run.leased_at < cutoff).with_for_update(skip_locked=True)).all()
        for row in expired:
            row.status = "waiting" if row.attempts < 3 else "system_error"
            row.assigned_node_id = row.lease_token = row.leased_at = None
            row.progress_current = 0
            if row.payload.get("collect_all_results"):
                row.result = None
            if row.status == "system_error":
                row.result = {"final_status": "system_error", "judge_message": "채점기 연결이 반복해서 중단되었습니다."}
                row.completed_at = now_utc()
        db.flush()
        from app.services.editor_execution import available_slots
        max_count = min(max_count, available_slots(db, node)) if node.allow_editor else 0
        if max_count <= 0:
            db.commit()
            return []
        query = select(Run).where(Run.status == "waiting")
        if ICPC_CAPABILITY not in (node.execution_capabilities or []):
            query = query.where(Run.payload["language"].as_string().not_in(NEW_LANGUAGES))
        if FULL_VERIFICATION not in (node.execution_capabilities or []):
            query = query.where(Run.payload["collect_all_results"].as_boolean().is_not(True))
        rows = db.scalars(query.order_by(Run.created_at, Run.run_id).limit(min(max_count, settings.judge_claim_max_batch_size)).with_for_update(skip_locked=True)).all()
        jobs = []
        for row in rows:
            row.status, row.assigned_node_id, row.lease_token, row.leased_at = "preparing", node_id, str(uuid4()), now_utc()
            row.node_name, row.agent_version = node.node_name, node.agent_version
            row.attempts += 1
            payload = row.payload
            jobs.append({
                "judge_job_id": row.run_id, "lease_token": row.lease_token,
                "submission": {"submission_id": row.run_id, "submission_kind": "editor_verification", "language": payload["language"], "source_code": payload["source_code"]},
                "collect_all_results": bool(payload.get("collect_all_results")),
                "problem": {"problem_id": row.project_id, "time_limit_ms": payload["time_limit_ms"], "memory_limit_mb": payload["memory_limit_mb"], "language_resource_limits": {}},
                # The bundle is an optimization. Keep real per-object fallback
                # references so a download failure cannot become a no-test AC.
                "testcases": [{**c, "input_url": object_storage.internal_presigned_get_url(c["input_storage_key"]),
                               "output_url": object_storage.internal_presigned_get_url(c["output_storage_key"])} for c in payload["testcases"]],
                "bundle_url": object_storage.internal_presigned_get_url(payload["bundle_key"]),
                "package_files": [{**r, "asset_id": r["sha256"], "url": object_storage.internal_presigned_get_url(r["storage_key"])} for r in payload["resources"]],
            })
        db.commit()
        return jobs


def output_key(row, index, sha):
    return f"editor-judge/{row.owner_id}/outputs/{row.run_id}/{index}-{sha}.out"


def merge_case_results(row, values, terminal=None):
    expected = {case["display_order"] for case in row.payload["testcases"]}
    previous = {case["index"]: case for case in (row.result or {}).get("testcase_results", [])}
    merged = dict(previous)
    writes = {}
    incoming = set()
    for item in values:
        value = CaseResult.model_validate(item).model_dump()
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
            size = len(content)
            original_size = value["output_total_bytes"]
            if size > MAX_CASE_OUTPUT_BYTES or original_size is None or original_size < size or value["output_truncated"] != (size < original_size):
                raise ValueError("lease mismatch: invalid output size")
            sha = hashlib.sha256(content).hexdigest()
            value.update(output_size=size, output_sha256=sha)
            key = output_key(row, index, sha)
            if key not in row.storage_keys:
                writes[key] = content
        if index in previous and previous[index] != value:
            raise ValueError("lease mismatch: testcase result changed")
        merged[index] = value
    if sum(case.get("output_size", 0) for case in merged.values()) > MAX_RUN_OUTPUT_BYTES:
        raise ValueError("lease mismatch: output budget exceeded")
    complete = set(merged) == expected
    if terminal is not None:
        if not complete and terminal["final_status"] not in {"compile_error", "system_error"}:
            raise ValueError("lease mismatch: incomplete full-suite result")
        if complete:
            first = next((case for _, case in sorted(merged.items()) if case["final_status"] != "accepted"), None)
            if terminal["final_status"] != (first["final_status"] if first else "accepted") or terminal.get("failed_testcase_order") != (first["index"] if first else None):
                raise ValueError("lease mismatch: aggregate differs from testcase results")
    for key, content in writes.items():
        if object_storage.size_bytes(key) is None:
            object_storage.write_bytes(key, content, "application/octet-stream")
    row.storage_keys = list(dict.fromkeys([*row.storage_keys, *writes]))
    return [value for _, value in sorted(merged.items())], complete


def testcase_output(email, run_id, index):
    with SessionLocal() as db:
        account = owner(db, email)
        row = db.scalar(select(Run).where(Run.run_id == run_id, Run.owner_id == account.account_id))
        case = next((case for case in (row.result or {}).get("testcase_results", []) if case["index"] == index), None) if row else None
        if not case or not case.get("output_available"):
            raise AppError(404, "editor_output_missing", "저장된 실제 출력이 없습니다.")
        content = object_storage.read_bytes(output_key(row, index, case["output_sha256"]))
        if len(content) != case["output_size"] or hashlib.sha256(content).hexdigest() != case["output_sha256"]:
            raise AppError(502, "editor_output_invalid", "저장된 출력의 무결성을 확인하지 못했습니다.")
        return {"base64": base64.b64encode(content).decode("ascii"), "sha256": case["output_sha256"],
                "size": len(content), "total_bytes": case["output_total_bytes"], "truncated": case["output_truncated"]}


def update(run_id, node_secret, lease_token, *, result=None, status=None, current=None, total=None, testcase_result=None, testcase_results=None, session_factory=None):
    with (session_factory or SessionLocal)() as db:
        row = db.scalar(select(Run).where(Run.run_id == run_id).with_for_update())
        if not row:
            return None
        node = db.get(JudgeNodeRow, row.assigned_node_id) if row.assigned_node_id else None
        if not valid_node_credential(node, node_secret):
            raise ValueError("node secret invalid")
        if not row.lease_token or not hmac.compare_digest(row.lease_token.encode(), lease_token.encode()):
            raise ValueError("lease mismatch")
        if result is not None and row.status not in ACTIVE:
            retry = {k: v for k, v in (row.result or {}).items() if k not in {"testcase_results", "full_suite_complete"}}
            incoming = {k: v for k, v in result.items() if k != "testcase_results"}
            if retry == incoming and (not row.payload.get("collect_all_results") or (merge_case_results(row, result.get("testcase_results") or [], result)[0] == row.result.get("testcase_results"))):
                return {"accepted": True, "editor_run": public_run(row)}
            raise ValueError("lease mismatch")
        if row.status not in RUNNING or not row.leased_at or aware(row.leased_at) <= now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds):
            raise ValueError("lease mismatch")
        row.leased_at = now_utc()
        if result is not None:
            if result["final_status"] not in {s.value for s in JUDGE_FINAL_STATUSES}:
                raise ValueError("invalid final status")
            failed = result.get("failed_testcase_order")
            if failed is not None and failed not in {c["display_order"] for c in row.payload["testcases"]}:
                raise ValueError("lease mismatch")
            row.status = result["final_status"]
            if row.payload.get("collect_all_results"):
                cases, complete = merge_case_results(row, result.get("testcase_results") or [], result)
                row.result = {**result, "testcase_results": cases, "full_suite_complete": complete}
                row.progress_current = len(cases)
            else:
                if result.get("testcase_results") is not None:
                    raise ValueError("lease mismatch: unrequested testcase results")
                row.result = result
            row.completed_at = now_utc()
            if row.status == "accepted":
                row.progress_current = row.testcase_count
        elif status:
            if status not in RUNNING or total not in {None, row.testcase_count}:
                raise ValueError("lease mismatch")
            row.status = status
            row.progress_current = max(row.progress_current, min(max(current or 0, 0), row.testcase_count))
            if testcase_result is not None or testcase_results is not None:
                if not row.payload.get("collect_all_results"):
                    raise ValueError("lease mismatch: unrequested testcase result")
                if testcase_result is not None and testcase_results is not None:
                    raise ValueError("lease mismatch: ambiguous testcase result format")
                cases, _ = merge_case_results(row, [testcase_result] if testcase_result is not None else testcase_results)
                row.result = {"testcase_results": cases, "full_suite_complete": False}
                row.progress_current = len(cases)
        db.commit()
        if result is not None and row.payload["bundle_key"] != row.payload["base_bundle_key"]:
            # Only a derived subset is disposable here. The immutable dataset
            # remains available for later subsets and other solution runs.
            try:
                for key in row.storage_keys:
                    object_storage.delete(key)
                row.storage_keys = []
                db.commit()
            except Exception:
                db.rollback()  # The final result above is already committed.
        return {"accepted": True, "editor_run": public_run(row)}
