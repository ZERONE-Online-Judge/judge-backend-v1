import json
import re
from contextlib import asynccontextmanager
import anyio.to_thread
from collections.abc import Mapping
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone
from fastapi.encoders import jsonable_encoder
from app.database import SessionLocal
from app import orm_models

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError

from app.routers import admin, analytics, auth, internal_judge, operator, participant, public, presentation, seo, seo_documents, storage
from app.routers import problem_archives, verification_ai, editor_bridge, editor_library, editor_mail, editor_auth, editor_judging
from app.services.errors import AppError
from app.services.audit_context import OPERATOR_PATH, VERIFICATION_PATH, operator_snapshot, verification_snapshot
from app.services.authz import bearer_token
from app.services.store import store
from app.settings import settings
from app.services.request_capacity import RequestCapacityMiddleware, busy_payload
from sqlalchemy.exc import DBAPIError, TimeoutError as DatabasePoolTimeout


@asynccontextmanager
async def lifespan(app):
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = settings.api_threadpool_tokens
    try:
        yield
    finally:
        limiter.total_tokens = previous


app = FastAPI(title="Zerone Online Judge API", version="0.1.0", lifespan=lifespan)

AUDITED_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
AUDIT_BODY_MAX_BYTES = 256 * 1024
AUDIT_REDACTED_KEYS = {
    "access_token",
    "node_secret",
    "password",
    "new_password",
    "code",
    "otp_code",
    "secret",
    "refresh_token",
    "session_token",
    "source_code",
    "token",
}
AUDIT_TRUNCATE_STRING_LENGTH = 4000
ID_SEGMENT_PATTERN = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

cors_origins = [origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()]




@app.exception_handler(DatabasePoolTimeout)
async def database_busy_handler(request: Request, exc: DatabasePoolTimeout):
    return JSONResponse(status_code=503, content=busy_payload(request.state.request_id),
        headers={"Retry-After": "1", "Cache-Control": "no-store"})


@app.exception_handler(DBAPIError)
async def database_statement_error(request: Request, exc: DBAPIError):
    if getattr(exc.orig, "sqlstate", None) not in {"57014", "55P03"}:
        raise exc
    return JSONResponse(status_code=503, content=busy_payload(request.state.request_id),
        headers={"Retry-After": "1", "Cache-Control": "no-store"})


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        headers={"Retry-After": "1", "Cache-Control": "no-store"} if exc.code == "api_busy" else None,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request.state.request_id,
                "details": exc.details,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Validation errors must not echo passwords, OTPs, tokens or uploaded code.
    return JSONResponse(status_code=422,content={"detail":[{key:item[key] for key in ("loc","msg","type") if key in item} for item in exc.errors()]})


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id", f"req_{uuid4().hex}")
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.request_id
    if request.url.path.startswith(('/api/auth/','/api/editor/auth/')):
        response.headers['Cache-Control']='no-store'
    return response


def _audit_scope(path: str) -> str | None:
    if path.startswith("/api/admin/"):
        return "admin"
    if path.startswith("/api/operator/"):
        return "operator"
    return None


def _audit_contest_id(path: str) -> str | None:
    match = re.match(r"^/api/(?:operator|admin)/contests/([^/]+)", path)
    return match.group(1) if match else None


def _audit_action(method: str, path: str) -> str:
    if method.upper() == "POST" and re.fullmatch(r"/api/operator/contests/[^/]+/operators/[^/]+/account:replace", path):
        return "운영자 계정 변경"
    if method.upper() == "POST" and re.search(r"/editor-problems/[a-f0-9]{32}/updates:preview$", path):
        return "Editor 패키지 업데이트 검토"
    if method.upper() == "POST" and re.search(r"/editor-problems/[a-f0-9]{32}/updates$", path):
        return "Editor 패키지 업데이트"
    if method.upper() == "DELETE" and re.fullmatch(r"/api/admin/contests/[^/]+", path):
        return "대회 삭제"
    if method.upper() == "POST" and re.search(r"/editor-problems/[a-f0-9]{32}/import$", path):
        return "Editor 문제 가져오기"
    if method.upper() == "POST" and re.search(r"/editor-problems/[a-f0-9]{32}/packages$", path):
        return "Editor 검증 패키지 준비"
    normalized_path = ID_SEGMENT_PATTERN.sub("/{id}", path)
    return f"{method.upper()} {normalized_path.removeprefix('/api')}"


def _audit_contest_title(contest_id: str | None) -> str | None:
    if not contest_id:
        return None
    from sqlalchemy import select
    from app.orm_models import ContestRow
    with store._session() as db:
        return db.scalar(select(ContestRow.title).where(ContestRow.contest_id == contest_id)
            .execution_options(include_deleted_contests=True))


def _audit_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > AUDIT_TRUNCATE_STRING_LENGTH:
            omitted = len(value) - AUDIT_TRUNCATE_STRING_LENGTH
            return f"{value[:AUDIT_TRUNCATE_STRING_LENGTH]}...(생략 {omitted}자)"
        return value
    if isinstance(value, list):
        if len(value) > 12:
            return [_audit_value(item) for item in value[:12]] + [
                f"...외 {len(value) - 12}개",
            ]
        return [_audit_value(item) for item in value]
    if isinstance(value, Mapping):
        return _audit_mapping(value)
    return value


def _audit_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        normalized_key = str(key)
        if normalized_key.lower() in AUDIT_REDACTED_KEYS:
            sanitized[normalized_key] = "기록 제외"
        else:
            sanitized[normalized_key] = _audit_value(value)
    return sanitized


def _audit_equal(left: Any, right: Any, field: str) -> bool:
    if field == "roles" and isinstance(left, list) and isinstance(right, list):
        return sorted(left) == sorted(right)
    if field.endswith("_at") and isinstance(left, str) and isinstance(right, str):
        try:
            return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(right.replace("Z", "+00:00"))
        except ValueError:
            pass
    return left == right


def _audit_changes(data: Any, before: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    changes = []
    for key, value in data.items():
        if key in {"created_at", "updated_at", "published_at"} or key.lower() in AUDIT_REDACTED_KEYS:
            continue
        if before is not None and key in before and _audit_equal(before[key], value, key):
            continue
        item = {"field": key, "new": _audit_value(value)}
        if before is not None and key in before:
            item["old"] = _audit_value(before[key])
        if item["new"] != value or (before is not None and key in before and item.get("old") != before[key]):
            item["truncated"] = True
        changes.append(item)
    return changes


def _audit_model_dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else None
    return None


def _audit_existing_values(path: str, body=None, *, after=False) -> dict[str, Any] | None:
    if OPERATOR_PATH.fullmatch(path):
        with SessionLocal() as db:
            return operator_snapshot(db, path, body, after=after)
    # Read persisted columns, not rendered notices, defaults, or request values.
    patterns = [
        (r"/operator/contests/([^/]+)/settings", orm_models.ContestRow),
        (r"/operator/contests/[^/]+/divisions/([^/]+)", orm_models.ContestDivisionRow),
        (r"/operator/contests/[^/]+/notices/([^/]+)", orm_models.ContestNoticeRow),
        (r"/operator/contests/[^/]+/participants/([^/]+)", orm_models.ParticipantTeamRow),
        (r"/operator/contests/[^/]+/participants/[^/]+/members/([^/]+)", orm_models.TeamMemberRow),
        (r"/operator/contests/[^/]+/problems/([^/]+)", orm_models.ProblemRow),
        (r"/operator/contests/[^/]+/problems/[^/]+/assets/([^/]+)", orm_models.ProblemAssetRow),
        (r"/operator/contests/[^/]+/problems/[^/]+/testcase-sets/([^/]+)", orm_models.TestcaseSetRow),
        (r"/operator/contests/[^/]+/problems/[^/]+/testcase-sets/[^/]+/testcases/([^/]+)", orm_models.TestcaseRow),
        (r"/operator/contests/[^/]+/boards/([^/]+)", orm_models.ContestQuestionRow),
        (r"/operator/contests/[^/]+/boards/[^/]+/answers/([^/]+)", orm_models.ContestQuestionAnswerRow),
        (r"/admin/service-notices/([^/]+)", orm_models.ServiceNoticeRow),
    ]
    for pattern, model in patterns:
        match = re.fullmatch("/api" + pattern, path)
        if match:
            with SessionLocal() as db:
                row = db.get(model, match.group(1))
                if row is None:
                    return None
                contest_id = _audit_contest_id(path)
                if hasattr(row, "contest_id") and row.contest_id != contest_id:
                    return None
                problem_match = re.search(r"/problems/([^/]+)", path)
                if problem_match:
                    problem = db.get(orm_models.ProblemRow, problem_match.group(1))
                    if not problem or problem.contest_id != contest_id:
                        return None
                    if hasattr(row, "problem_id") and row.problem_id != problem.problem_id:
                        return None
                    if model is orm_models.TestcaseRow:
                        case_set = db.get(orm_models.TestcaseSetRow, row.testcase_set_id)
                        set_match = re.search(r"/testcase-sets/([^/]+)", path)
                        if not case_set or case_set.problem_id != problem.problem_id or not set_match or case_set.testcase_set_id != set_match.group(1):
                            return None
                if model is orm_models.TeamMemberRow:
                    team_match = re.search(r"/participants/([^/]+)", path)
                    if not team_match or row.participant_team_id != team_match.group(1):
                        return None
                values = {}
                for column in model.__table__.columns:
                    value = getattr(row, column.name)
                    if isinstance(value, datetime):
                        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
                    values[column.name] = jsonable_encoder(value)
                return values
    return None


def _audit_path_entities(path: str) -> dict[str, str]:
    patterns = [
        (r"^/api/operator/contests/([^/]+)/settings$", ["contest_id"]),
        (r"^/api/operator/contests/([^/]+)/divisions/([^/]+)$", ["contest_id", "division_id"]),
        (r"^/api/operator/contests/([^/]+)/operators/([^/]+)(?:/account:replace)?$", ["contest_id", "operator_email"]),
        (r"^/api/operator/contests/([^/]+)/notices/([^/]+)$", ["contest_id", "notice_id"]),
        (r"^/api/operator/contests/([^/]+)/boards/([^/]+)$", ["contest_id", "question_id"]),
        (
            r"^/api/operator/contests/([^/]+)/boards/([^/]+)/answers/([^/]+)$",
            ["contest_id", "question_id", "answer_id"],
        ),
        (r"^/api/operator/contests/([^/]+)/participants/([^/]+)$", ["contest_id", "participant_team_id"]),
        (
            r"^/api/operator/contests/([^/]+)/participants/([^/]+)/members/([^/]+)(?:/sessions:revoke)?$",
            ["contest_id", "participant_team_id", "team_member_id"],
        ),
        (r"^/api/operator/contests/([^/]+)/problems/([^/]+)$", ["contest_id", "problem_id"]),
        (
            r"^/api/operator/contests/([^/]+)/problems/([^/]+)/test-submissions$",
            ["contest_id", "problem_id"],
        ),
        (
            r"^/api/operator/contests/([^/]+)/problems/([^/]+)/assets/([^/]+)$",
            ["contest_id", "problem_id", "asset_id"],
        ),
        (
            r"^/api/operator/contests/([^/]+)/problems/([^/]+)/testcase-sets/([^/]+)$",
            ["contest_id", "problem_id", "testcase_set_id"],
        ),
        (
            r"^/api/operator/contests/([^/]+)/problems/([^/]+)/testcase-sets/([^/]+)/testcases/([^/]+)$",
            ["contest_id", "problem_id", "testcase_set_id", "testcase_id"],
        ),
        (r"^/api/admin/contests/([^/]+)/operators$", ["contest_id"]),
        (r"^/api/admin/contests/([^/]+)/divisions$", ["contest_id"]),
        (r"^/api/admin/service-notices/([^/]+)$", ["service_notice_id"]),
        (r"^/api/admin/contact-inquiries/([^/]+)/answer$", ["contact_inquiry_id"]),
    ]
    for pattern, keys in patterns:
        match = re.match(pattern, path)
        if match:
            return dict(zip(keys, match.groups(), strict=False))
    entities = {}
    for segment, field in (("contests", "contest_id"), ("divisions", "division_id"),
                           ("problems", "problem_id"), ("testcase-sets", "testcase_set_id")):
        match = re.search(r"/" + segment + r"/([^/]+)", path)
        if match:
            entities[field] = match.group(1)
    return entities


async def _audit_request_payload(request: Request) -> tuple[Request, dict[str, Any]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content_length = int(request.headers.get("content-length") or "0")
    if content_type != "application/json" or content_length > AUDIT_BODY_MAX_BYTES:
        return request, {}

    body = await request.body()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    replay_request = Request(request.scope, receive)
    if not body:
        return replay_request, {}

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return replay_request, {}

    return replay_request, {
        "body": _audit_value(parsed),
    }


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


@app.middleware("http")
async def operational_audit_middleware(request: Request, call_next):
    path = request.url.path
    scope = _audit_scope(path)
    payload_details: dict[str, Any] = {}
    account = None
    before = None
    if request.method.upper() in AUDITED_METHODS and scope:
        request, payload_details = await _audit_request_payload(request)
        payload_details["entities"] = _audit_path_entities(path)
        before = await run_in_threadpool(_audit_existing_values, path, payload_details.get("body"))
        payload_details["schema_version"] = 2
        # Keep the actor's identity at the time of the request, including when
        # the action changes their email and revokes the token being used.
        try:
            token = bearer_token(request)
            account = await run_in_threadpool(store.get_staff_by_access_token, token) if token else None
            if not account and token:
                account = await run_in_threadpool(store.get_staff_by_general_access_token, token)
        except Exception:
            pass

    response = await call_next(request)
    if response.status_code < 400 and getattr(request.state, "operator_access", None):
        from app.services.access_logging import record_operator_access
        contest_id, operator_account = request.state.operator_access
        await run_in_threadpool(record_operator_access, request, contest_id, operator_account, bearer_token(request) or "")
    if request.method.upper() not in AUDITED_METHODS or not scope or (response.status_code < 400 and getattr(request.state, "package_update_audited", False)):
        return response

    try:
        contest_id = _audit_contest_id(path)
        changes = []
        change_kind = "requested"
        if response.status_code >= 400:
            change_kind = "failed"
        elif request.method.upper() == "DELETE":
            change_kind = "deleted"
        elif OPERATOR_PATH.fullmatch(path):
            after = await run_in_threadpool(_audit_existing_values, path, payload_details.get("body"), after=True)
            if after is not None:
                changes = _audit_changes(after, before) if before is not None else []
                change_kind = "updated" if before is not None else "created"
        elif before is not None and request.method.upper() in {"PATCH", "PUT"}:
            after = await run_in_threadpool(_audit_existing_values, path)
            if after is not None:
                changes = _audit_changes(after, before)
                change_kind = "updated"
        target_values = before
        if response.status_code >= 400:
            target_values = None
        elif OPERATOR_PATH.fullmatch(path) and request.method.upper() != "DELETE":
            target_values = after
        extra = {}
        if response.status_code < 400 and getattr(request.state, "editor_import_details", None):
            extra = request.state.editor_import_details
        if response.status_code < 400 and getattr(request.state, "audit_rejudge", None):
            extra["rejudge"] = request.state.audit_rejudge
        if response.status_code < 400 and VERIFICATION_PATH.fullmatch(path):
            def read_verification_audit():
                with SessionLocal() as db:
                    return verification_snapshot(db, path, getattr(request.state, "audit_result", None))
            extra = await run_in_threadpool(read_verification_audit)
        details: dict[str, Any] = {
            **payload_details,
            "changes": changes,
            "change_kind": change_kind,
            "target": _audit_mapping({key: target_values[key] for key in ("title", "name", "team_name", "problem_code", "original_filename", "display_order", "display_name", "email", "roles") if key in target_values}) if target_values else {},
            "contest_title": await run_in_threadpool(_audit_contest_title, contest_id),
            **_audit_mapping(extra),
        }
        if request.url.query:
            details["query"] = request.url.query
        actor_role = None
        if account:
            actor_role = "service_master" if account.is_service_master else "operator"
        await run_in_threadpool(store.append_operational_audit_log,
            scope=scope,
            action=_audit_action(request.method, path),
            method=request.method.upper(),
            path=path,
            status_code=response.status_code,
            actor_email=str(account.email) if account else None,
            actor_name=account.display_name if account else None,
            actor_role=actor_role,
            contest_id=contest_id,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
            details={key: value for key, value in details.items() if value not in (None, "", [], {})},
        )
    except Exception:
        # Audit logging must never break the operator/admin action itself.
        pass
    return response


@app.get("/api/health")
async def health(request: Request):
    return {
        "data": {
            "status": "ok",
            "env": settings.app_env,
            "release_color": settings.release_color,
            "release_version": settings.release_version,
        },
        "request_id": request.state.request_id,
    }


app.include_router(public.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(seo.router, prefix="/api")
app.include_router(seo_documents.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(participant.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(operator.router, prefix="/api")
app.include_router(problem_archives.router, prefix="/api")
app.include_router(editor_bridge.router, prefix="/api")
app.include_router(editor_library.router, prefix="/api")
app.include_router(editor_mail.router, prefix="/api")
app.include_router(editor_auth.router, prefix="/api")
app.include_router(editor_judging.router, prefix="/api")
app.include_router(verification_ai.router, prefix="/api")
app.include_router(internal_judge.router, prefix="/api")
app.include_router(storage.router, prefix="/api")

app.include_router(presentation.router, prefix="/api")


# Admission precedes audit/auth work; CORS also applies to overload responses.
app.add_middleware(RequestCapacityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
