"""Private, signed Editor execution requests; no contest permissions or writes."""
import asyncio
import hashlib
import tempfile
import threading

from fastapi import APIRouter, Query, Request, Path
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.services import editor_judging as judging, editor_execution as execution
from app.services.editor_bridge import identity
from app.services.errors import AppError
from app.services.responses import ok

router = APIRouter(prefix="/editor/judge", tags=["editor judge integration"])
_upload_slots = threading.BoundedSemaphore(2)


def authenticated(request, empty=False):
    email, digest = identity(request)
    if empty and digest != hashlib.sha256(b"").hexdigest():
        raise AppError(401, "editor_content_mismatch", "요청 내용이 인증 정보와 다릅니다.")
    with SessionLocal() as db:
        judging.owner(db, email)
    return email, digest


@router.get("/capabilities")
def capabilities(request: Request):
    authenticated(request, empty=True)
    return ok(request, {**judging.capabilities(), "execution": execution.capabilities()})


@router.post("/runs", status_code=202)
async def create_run(request: Request):
    email, expected = authenticated(request)
    if request.headers.get("content-type", "").split(";")[0] not in {"application/zip", "application/octet-stream"}:
        raise AppError(415, "editor_judge_snapshot_invalid", "ZIP 채점 자료를 전송하세요.")
    if not _upload_slots.acquire(blocking=False):
        raise AppError(429, "editor_judge_busy", "채점 자료를 처리 중입니다. 잠시 후 다시 요청하세요.")
    try:
        with tempfile.TemporaryFile("w+b") as source:
            size, digest = 0, hashlib.sha256()
            async for chunk in request.stream():
                size += len(chunk)
                if size > judging.MAX_ARCHIVE:
                    raise AppError(413, "editor_judge_archive_large", "채점 자료 ZIP은 512MiB 이하여야 합니다.")
                digest.update(chunk)
                await run_in_threadpool(source.write, chunk)
            if digest.hexdigest() != expected:
                raise AppError(401, "editor_content_mismatch", "채점 자료가 서명한 내용과 다릅니다.")
            source.seek(0)
            return ok(request, await run_in_threadpool(judging.create, email, source, expected))
    finally:
        _upload_slots.release()


@router.get("/runs/{run_id}")
def run_status(run_id: str, request: Request):
    email, _ = authenticated(request, empty=True)
    return ok(request, judging.get_run(email, run_id))


@router.post("/runs/{run_id}/remaining", status_code=202)
def remaining(run_id: str, request: Request, operation_id: str = Query(pattern=r"^[a-f0-9]{32}$")):
    email, _ = authenticated(request, empty=True)
    return ok(request, judging.resume(email, run_id, operation_id))


@router.get("/runs/{run_id}/testcases/{index}/output")
def testcase_output(run_id: str, index: int, request: Request):
    email, _ = authenticated(request, empty=True)
    return ok(request, judging.testcase_output(email, run_id, index))


@router.delete("/runs/{run_id}")
def cancel(run_id: str, request: Request):
    email, _ = authenticated(request, empty=True)
    return ok(request, judging.get_run(email, run_id, cancel=True))


@router.delete("/run-operations/{operation_id}")
def cancel_run_operation(request: Request, operation_id: str = Path(pattern=r"^[a-f0-9]{32}$")):
    email, _ = authenticated(request, empty=True)
    return ok(request, judging.cancel_operation(email, operation_id))


@router.post("/tasks", status_code=202)
async def create_task(request: Request):
    email, expected = authenticated(request)
    if request.headers.get("content-type", "").split(";")[0] != "application/zip":
        raise AppError(415, "editor_execution_invalid", "ZIP 실행 자료를 전송하세요.")
    if not _upload_slots.acquire(blocking=False):
        raise AppError(429, "editor_execution_busy", "실행 자료를 처리 중입니다. 잠시 후 다시 요청하세요.")
    try:
        with tempfile.TemporaryFile("w+b") as source:
            size, digest = 0, hashlib.sha256()
            async for chunk in request.stream():
                size += len(chunk)
                if size > execution.MAX_WIRE_BYTES:
                    raise AppError(413, "editor_execution_large", "실행 자료가 전송 한도를 넘었습니다.")
                digest.update(chunk)
                await run_in_threadpool(source.write, chunk)
            if digest.hexdigest() != expected:
                raise AppError(401, "editor_content_mismatch", "실행 자료가 서명한 내용과 다릅니다.")
            source.seek(0)
            content = await run_in_threadpool(source.read)
            return ok(request, await run_in_threadpool(execution.create, email, content, expected))
    finally:
        _upload_slots.release()


@router.get("/tasks/{task_id}")
async def task_status(task_id: str, request: Request, wait_seconds: float = Query(default=0, ge=0, le=20)):
    email, _ = authenticated(request, empty=True)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        task = await run_in_threadpool(execution.get, email, task_id)
        if task["status"] not in execution.ACTIVE or asyncio.get_running_loop().time() >= deadline:
            return ok(request, task)
        await asyncio.sleep(min(0.2, max(0, deadline - asyncio.get_running_loop().time())))


@router.delete("/tasks/{task_id}")
def cancel_task(task_id: str, request: Request):
    email, _ = authenticated(request, empty=True)
    return ok(request, execution.get(email, task_id, cancel=True))


@router.get("/execution-preflight")
def execution_preflight(request: Request):
    _, digest = identity(request)
    if digest != hashlib.sha256(b"").hexdigest():
        raise AppError(401, "editor_content_mismatch", "요청 내용이 인증 정보와 다릅니다.")
    return ok(request, execution.capabilities())



@router.delete("/task-operations/{operation_id}")
def cancel_task_operation(request: Request, operation_id: str = Path(pattern=r"^[a-f0-9]{32}$")):
    email, _ = authenticated(request, empty=True)
    return ok(request, execution.cancel_operation(email, operation_id))
