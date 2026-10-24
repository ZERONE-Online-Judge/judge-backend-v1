import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import JUDGE_FINAL_STATUSES, JUDGE_PROGRESS_STATUSES, SubmissionStatus
from app.settings import settings
from app.services.errors import AppError, not_found
from app.services.responses import ok
from app.services.store import store
from app.services import editor_judging, editor_execution, verification_execution

router = APIRouter(tags=["internal-judge"])


class NodeCredentialRequest(BaseModel):
    node_secret: str = Field(min_length=1, max_length=1024)


class RegisterNodeRequest(NodeCredentialRequest):
    node_name: str = Field(min_length=1, max_length=120)
    total_slots: int = Field(default=10, ge=1, le=1024)
    agent_version: str = Field(default="0.1.0", max_length=64)
    execution_capabilities: list[str] = Field(default_factory=list, max_length=10)
    execution_revision: str | None = Field(default=None, max_length=128)

    @field_validator("execution_capabilities")
    @classmethod
    def supported_capabilities(cls, value):
        if any(item not in {"editor-execute-v1", editor_judging.FULL_VERIFICATION, editor_judging.REFERENCE_OUTPUT, verification_execution.CAPABILITY, "judge-icpc-languages-v1", "editor-icpc-languages-v1", "judge-zoj-runtime-v1", "editor-zoj-runtime-v1"} for item in value):
            raise ValueError("Unsupported execution capability")
        return sorted(set(value))


    @model_validator(mode="after")
    def runtime_revision_required(self):
        if self.execution_capabilities and not self.execution_revision:
            raise ValueError("Runtime revision is required for Editor execution")
        return self


class HeartbeatRequest(NodeCredentialRequest):
    total_slots: int = Field(ge=1, le=1024)
    free_slots: int = Field(ge=0, le=1024)
    running_job_count: int = Field(ge=0, le=1024)
    agent_version: str | None = Field(default=None, max_length=64)


class ClaimRequest(NodeCredentialRequest):
    max_count: int = Field(default=1, ge=1, le=100)
    wait_seconds: float = Field(default=0.0, ge=0, le=60)


class ResultRequest(NodeCredentialRequest):
    lease_token: str = Field(min_length=1, max_length=128)
    final_status: SubmissionStatus
    compile_message: str | None = None
    judge_message: str | None = None
    failed_testcase_order: int | None = None
    runtime_ms: int | None = None
    memory_kb: int | None = None
    testcase_results: list[editor_judging.CaseResult] | None = Field(default=None, max_length=1000)

    @field_validator("final_status")
    @classmethod
    def terminal_status_only(cls, value):
        if value not in JUDGE_FINAL_STATUSES:
            raise ValueError("Result must be a terminal judge status")
        return value


class ProgressRequest(NodeCredentialRequest):
    lease_token: str = Field(min_length=1, max_length=128)
    status: SubmissionStatus
    progress_current: int | None = None
    progress_total: int | None = None
    testcase_result: editor_judging.CaseResult | None = None
    testcase_results: list[editor_judging.CaseResult] | None = Field(default=None, max_length=1000)

    @field_validator("status")
    @classmethod
    def progress_status_only(cls, value):
        if value not in JUDGE_PROGRESS_STATUSES:
            raise ValueError("Progress must be preparing or judging")
        return value


class LeaseRenewRequest(NodeCredentialRequest):
    lease_token: str = Field(min_length=1, max_length=128)


class AgentLogItem(BaseModel):
    level: str = Field(default="info", max_length=16)
    message: str = Field(max_length=8000)


class AgentLogsRequest(NodeCredentialRequest):
    logs: list[AgentLogItem] = Field(max_length=300)


@router.post("/internal/judge/nodes/register")
async def register_node(payload: RegisterNodeRequest, request: Request):
    try:
        node = await asyncio.to_thread(
            store.register_node,
            payload.node_name,
            payload.node_secret,
            payload.total_slots,
            payload.agent_version,
            payload.execution_capabilities,
            payload.execution_revision,
        )
    except ValueError:
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
    return ok(request, {"judge_node_id": node.judge_node_id, "heartbeat_interval_seconds": 2})


@router.post("/internal/judge/nodes/{node_id}/heartbeat")
async def heartbeat(node_id: str, payload: HeartbeatRequest, request: Request):
    try:
        node = await asyncio.to_thread(
            store.update_node_heartbeat,
            node_id,
            payload.node_secret,
            payload.total_slots,
            payload.free_slots,
            payload.running_job_count,
            payload.agent_version,
        )
    except ValueError:
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
    if not node:
        raise not_found()
    return ok(request, node.model_dump(mode="json"))


@router.post("/internal/judge/nodes/{node_id}/logs")
async def append_node_logs(node_id: str, payload: AgentLogsRequest, request: Request):
    try:
        accepted = await asyncio.to_thread(
            store.append_judge_agent_logs,
            node_id,
            payload.node_secret,
            [item.model_dump() for item in payload.logs],
        )
    except ValueError:
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
    if accepted is None:
        raise not_found()
    return ok(request, {"accepted": accepted})


@router.post("/internal/judge/nodes/{node_id}/assignments:claim")
async def claim(node_id: str, payload: ClaimRequest, request: Request):
    deadline = min(max(payload.wait_seconds, 0.0), settings.judge_claim_max_wait_seconds)
    started = asyncio.get_running_loop().time()
    while True:
        try:
            jobs = await asyncio.to_thread(store.claim_jobs, node_id, payload.node_secret, payload.max_count)
            if jobs is not None:
                # Existing contest/rejudge/AI work is always claimed first.
                remaining = min(payload.max_count, settings.judge_claim_max_batch_size) - len(jobs)
                if remaining > 0:
                    from app.services import editor_execution
                    jobs += await asyncio.to_thread(editor_execution.claim_jobs, node_id, payload.node_secret, remaining, session_factory=store._session)
                    remaining = min(payload.max_count, settings.judge_claim_max_batch_size) - len(jobs)
                    if remaining > 0:
                        jobs += await asyncio.to_thread(editor_judging.claim_jobs, node_id, payload.node_secret, remaining, session_factory=store._session)
        except ValueError:
            raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
        if jobs is None:
            raise not_found()
        if jobs or deadline <= 0:
            break
        elapsed = asyncio.get_running_loop().time() - started
        if elapsed >= deadline:
            break
        await asyncio.sleep(min(settings.judge_claim_poll_interval_seconds, deadline - elapsed))
    if jobs is None:
        raise not_found()
    from app.language_limits import agent_problem
    jobs = [{**job, "problem": agent_problem(job["problem"])} if isinstance(job.get("problem"), dict) and "time_limit_ms" in job["problem"] else job for job in jobs]
    return ok(request, {"jobs": jobs})


@router.post("/internal/judge/jobs/{job_id}/result")
async def report_result(job_id: str, payload: ResultRequest, request: Request):
    try:
        result = await asyncio.to_thread(
            store.report_judge_result,
            job_id,
            payload.node_secret,
            payload.lease_token,
            payload.final_status,
            payload.compile_message,
            payload.judge_message,
            payload.failed_testcase_order,
            payload.runtime_ms,
            payload.memory_kb,
            testcase_results=[case.model_dump() for case in payload.testcase_results] if payload.testcase_results is not None else None,
        )
        if result is None:
            editor_result = await asyncio.to_thread(
                editor_judging.update, job_id, payload.node_secret, payload.lease_token,
                result={"final_status": payload.final_status.value, "compile_message": payload.compile_message,
                        "judge_message": payload.judge_message, "failed_testcase_order": payload.failed_testcase_order,
                        "runtime_ms": payload.runtime_ms, "memory_kb": payload.memory_kb,
                        **({"testcase_results": [case.model_dump() for case in payload.testcase_results]} if payload.testcase_results is not None else {})},
                session_factory=store._session,
            )
            if editor_result:
                return ok(request, editor_result)
    except ValueError as error:
        if "lease mismatch" in str(error):
            raise AppError(409, "lease_conflict", "Lease token mismatch.")
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
    if not result:
        raise not_found()
    submission, job = result
    return ok(request, {"accepted": True, "submission": submission.model_dump(mode="json"), "job": job.model_dump(mode="json")})


@router.post("/internal/judge/jobs/{job_id}/progress")
async def report_progress(job_id: str, payload: ProgressRequest, request: Request):
    try:
        result = await asyncio.to_thread(
            store.update_judge_progress,
            job_id,
            payload.node_secret,
            payload.lease_token,
            payload.status,
            payload.progress_current,
            payload.progress_total,
            testcase_result=payload.testcase_result.model_dump() if payload.testcase_result else None,
            testcase_results=[case.model_dump() for case in payload.testcase_results] if payload.testcase_results is not None else None,
        )
        if result is None:
            editor_result = await asyncio.to_thread(
                editor_judging.update, job_id, payload.node_secret, payload.lease_token,
                status=payload.status.value, current=payload.progress_current, total=payload.progress_total,
                testcase_result=payload.testcase_result.model_dump() if payload.testcase_result else None,
                testcase_results=[case.model_dump() for case in payload.testcase_results] if payload.testcase_results is not None else None,
                session_factory=store._session,
            )
            if editor_result:
                return ok(request, editor_result)
    except ValueError as error:
        if "lease mismatch" in str(error):
            raise AppError(409, "lease_conflict", "Lease token mismatch.")
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
    if not result:
        raise not_found()
    submission, job = result
    return ok(request, {"accepted": True, "submission": submission.model_dump(mode="json"), "job": job.model_dump(mode="json")})


@router.post("/internal/judge/jobs/{job_id}/lease:renew")
async def renew_lease(job_id: str, payload: LeaseRenewRequest, request: Request):
    try:
        job = await asyncio.to_thread(
            store.renew_judge_lease,
            job_id,
            payload.node_secret,
            payload.lease_token,
        )
        if job is None:
            editor_result = await asyncio.to_thread(editor_judging.update, job_id, payload.node_secret, payload.lease_token, session_factory=store._session)
            if editor_result:
                return ok(request, editor_result)
            task_result = await asyncio.to_thread(editor_execution.update, job_id, payload.node_secret, payload.lease_token, session_factory=store._session)
            if task_result:
                return ok(request, task_result)
    except ValueError as error:
        if "lease mismatch" in str(error):
            raise AppError(409, "lease_conflict", "Lease token mismatch.")
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
    if not job:
        raise not_found()
    return ok(request, {"accepted": True, "job": job.model_dump(mode="json")})


@router.post("/internal/judge/editor-tasks/{task_id}/result")
async def report_editor_task(task_id: str, request: Request):
    import json
    from app.services.editor_protocol import MAX_WIRE_BYTES
    secret, lease = request.headers.get("X-Judge-Node-Secret", ""), request.headers.get("X-Judge-Lease", "")
    encoded = request.headers.get("X-Judge-Node-Credential")
    if encoded is not None:
        import base64
        try:
            if len(encoded) > 6144:
                raise ValueError("Credential exceeds limit")
            secret = base64.b64decode(encoded, altchars=b'-_', validate=True).decode('utf-8')
        except (ValueError, UnicodeError):
            raise AppError(403, "node_secret_invalid", "Judge node credential is invalid.")
    if not 1 <= len(secret) <= 1024 or not 1 <= len(lease) <= 128:
        raise AppError(403, "node_secret_invalid", "Judge node credential is missing.")
    try:
        accepted = await asyncio.to_thread(editor_execution.update, task_id, secret, lease, session_factory=store._session)
        if not accepted:
            raise not_found()
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_WIRE_BYTES:
                raise AppError(413, "editor_result_large", "실행 결과가 전송 한도를 넘었습니다.")
            content.extend(chunk)
        try:
            value = json.loads(content)
        except (ValueError, UnicodeError):
            raise AppError(422, "editor_result_invalid", "JSON 실행 결과를 전송하세요.")
        if not isinstance(value, dict) or set(value) not in [{"result"}, {"error"}]:
            raise AppError(422, "editor_result_invalid", "실행 결과 또는 오류 하나를 전송하세요.")
        if "error" in value and (not isinstance(value["error"], str) or not value["error"] or len(value["error"]) > 4000):
            raise AppError(422, "editor_result_invalid", "실행 오류 형식을 확인하세요.")
        return ok(request, await asyncio.to_thread(editor_execution.update, task_id, secret, lease, **value, session_factory=store._session))
    except ValueError as error:
        if "lease mismatch" in str(error):
            raise AppError(409, "lease_conflict", "Lease token mismatch.")
        raise AppError(403, "node_secret_invalid", "Judge node secret is invalid.")
