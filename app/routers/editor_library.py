import hashlib
import json
import re
from urllib.parse import urlencode
from fastapi import APIRouter, Query, Request, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.routers.operator import _require_contest_mutation_open
from app.routers.problem_archives import _archive_slots, _reserve
from app.services.authz import require_contest_staff
from app.services import editor_library as transport
from app.services.errors import AppError
from app.services.problem_archive import import_archive
from app.services.responses import ok
from app.services import package_updates

router = APIRouter(prefix="/operator/contests", tags=["Editor library"])
PROJECT_ID = r"^[a-f0-9]{32}$"


def access(request, contest_id, mutate=False):
    account = require_contest_staff(request, contest_id, "contest.problem.manage")
    if mutate:
        _require_contest_mutation_open(contest_id)
    return account


def project_path(problem_id, job_id=None):
    return "/api/zoj-library/problems/" + problem_id + "/packages" + ("/" + job_id if job_id else "")


@router.get("/{contest_id}/editor-problems")
def projects(contest_id: str, request: Request, q: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0, le=100000), folder: str = Query(default="all", pattern=r"^(all|root|[a-f0-9]{32})$")):
    account = access(request, contest_id)
    if not transport.configured():
        return ok(request, {"configured": False, "email": account.email, "problems": [], "folders": [], "total": 0})
    data = transport.request_json("GET", "/api/zoj-library/problems?" + urlencode({"q": q, "offset": offset, "folder": folder}), str(account.email))
    if data.get("email", "").lower() != str(account.email).lower():
        raise AppError(502, "editor_identity_mismatch", "연결된 Editor 계정 정보가 다릅니다.")
    return ok(request, {"configured": True, **data})


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    testset: str = Field(default="tests", min_length=1, max_length=100)
    testsets: list[str] | None = Field(default=None, min_length=1, max_length=20)
    statement_language: str = Field(default="korean", min_length=1, max_length=100)


@router.post("/{contest_id}/editor-problems/{problem_id}/packages", status_code=202)
def prepare(contest_id: str, request: Request, values: Options, problem_id: str = Path(pattern=PROJECT_ID)):
    account = access(request, contest_id)
    return ok(request, transport.request_json("POST", project_path(problem_id), str(account.email), values.model_dump()))


@router.get("/{contest_id}/editor-problems/{problem_id}/packages/{job_id}")
def status(contest_id: str, request: Request, problem_id: str = Path(pattern=PROJECT_ID), job_id: str = Path(pattern=PROJECT_ID)):
    account = access(request, contest_id)
    return ok(request, transport.request_json("GET", project_path(problem_id, job_id), str(account.email)))


class Import(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=PROJECT_ID)
    division_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    problem_code: str = Field(min_length=1, max_length=16)

    @field_validator("problem_code")
    @classmethod
    def code(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid problem code")
        return value


@router.post("/{contest_id}/editor-problems/{problem_id}/import")
def import_project(contest_id: str, request: Request, values: Import, problem_id: str = Path(pattern=PROJECT_ID)):
    account = access(request, contest_id, True)
    email = str(account.email).lower()
    package = transport.request_json("GET", project_path(problem_id, values.job_id), email)
    metadata = package.get("package") or {}
    if package.get("status") != "completed" or not metadata.get("verified") or metadata.get("format") != "zoj":
        raise AppError(409, "editor_package_not_ready", "Editor 패키지 검증을 먼저 완료하세요.")
    sha = metadata.get("sha256", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise AppError(502, "editor_archive_invalid", "Editor 패키지 정보가 올바르지 않습니다.")
    operation = hashlib.sha256("\n".join([email, problem_id, values.job_id, contest_id, values.division_id, values.problem_code]).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([contest_id, values.division_id, values.problem_code, sha], ensure_ascii=False).encode()).hexdigest()
    _reserve()
    try:
        with transport.download_package(project_path(problem_id, values.job_id) + "/download", email, sha) as source:
            result = import_archive(source, contest_id, values.division_id, values.problem_code,
                editor_context={"email": email, "operation_key": operation, "request_digest": fingerprint,
                    "request_id": request.state.request_id, "skip_audit": True})
    finally:
        _archive_slots.release()
    request.state.editor_import_details = {"target": {"problem_title": result["title"], "problem_code": result["problem_code"]},
        "editor": {"problem_id": problem_id, "revision": package.get("revision"), "job_id": values.job_id,
                   "imported_problem_id": result["problem_id"]}}
    return ok(request, result)


class UpdatePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=PROJECT_ID)
    target_problem_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


class Update(UpdatePreview):
    target_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=1000)
    confirm_running: bool = False
    change_notice: dict | None = Field(default=None, alias="_change_notice")

    @field_validator("reason")
    @classmethod
    def reason_required(cls, value):
        if not value.strip():
            raise ValueError("업데이트 이유를 입력하세요.")
        return value.strip()


def verified_package(problem_id, job_id, email):
    row = transport.request_json("GET", project_path(problem_id, job_id), email)
    metadata = row.get("package") or {}
    if row.get("status") != "completed" or metadata.get("verified") is not True or metadata.get("format") != "zoj":
        raise AppError(409, "editor_package_not_ready", "Editor 패키지 검증을 먼저 완료하세요.")
    sha = metadata.get("sha256", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise AppError(502, "editor_archive_invalid", "Editor 패키지 정보가 올바르지 않습니다.")
    return row, sha


@router.post("/{contest_id}/editor-problems/{problem_id}/updates:preview")
def preview_update(contest_id: str, request: Request, values: UpdatePreview, problem_id: str = Path(pattern=PROJECT_ID)):
    account = access(request, contest_id)
    _, sha = verified_package(problem_id, values.job_id, str(account.email))
    _reserve()
    try:
        with transport.download_package(project_path(problem_id, values.job_id) + "/download", str(account.email), sha) as source:
            return ok(request, package_updates.preview(source, contest_id, values.target_problem_id))
    finally:
        _archive_slots.release()


@router.post("/{contest_id}/editor-problems/{problem_id}/updates")
def apply_update(contest_id: str, request: Request, values: Update, problem_id: str = Path(pattern=PROJECT_ID)):
    account = access(request, contest_id)
    email = str(account.email).lower()
    package, sha = verified_package(problem_id, values.job_id, email)
    if sha != values.archive_sha256:
        raise AppError(409, "package_update_archive_changed", "패키지가 변경되었습니다. 변경 내용을 다시 확인하세요.")
    operation = package_updates.digest([email, contest_id, problem_id, values.job_id, values.target_problem_id, values.target_hash])
    context = {"email": email, "operation_key": operation,
        "request_digest": package_updates.digest(values.model_dump(exclude={"change_notice"})), "request_id": request.state.request_id,
        "path": request.url.path,
        "source": {"problem_id": problem_id, "job_id": values.job_id, "revision": package.get("revision"), "sha256": sha}}
    _reserve()
    try:
        with transport.download_package(project_path(problem_id, values.job_id) + "/download", email, sha) as source:
            result = package_updates.apply(source, contest_id, values.target_problem_id, values.target_hash,
                sha, values.reason, values.confirm_running, context,
                change_notice=values.change_notice, require_notice=True)
    finally:
        _archive_slots.release()
    request.state.package_update_audited = True
    response = ok(request, {"problem": result, "backup_url": f"/api/operator/contests/{contest_id}/problems/{values.target_problem_id}/package-backups/{operation}",
        "revision": package.get("revision"), "official_results_unchanged": True})
    if context.get("change_notice"):
        response["change_notice"] = context["change_notice"]
    return response
