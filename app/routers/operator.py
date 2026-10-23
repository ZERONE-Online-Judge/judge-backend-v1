import asyncio
import re
import hashlib
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, UploadFile
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from app.problem_languages import ProblemStatements, StatementTranslation, validate_statement_fields

from app.models import ContestResourceAccess, ContestStatus, ContestVisibility, ProblemAsset, ScoreboardFreezeMode, ScoreboardReleaseMode, SubmissionStatus, TeamMemberRole, now_utc
from app.services.authz import has_contest_permission, is_contest_master, require_contest_staff, require_staff
from app.services.access_logging import write_access_log
from app.services.automatic_notices import time_update_notice_body
from app.services.change_notices import ChangeNoticeRoute, allows_live_problem_change, confirm_change
from app.services.contest_roles import ContestOperatorCreateRequest, ContestOperatorUpdateRequest, title_for_roles
from app.services.errors import AppError, not_found, permission_denied
from app.services.mail_templates import absolute_url, operator_assignment_mail, render_branded_email
from app.services.package_builder import PackageBuildError, package_role, is_editor_source_asset
from app.services.responses import ok, page
from app.services.mail_logs import list_mail_logs, mail_log_filters, mail_log_preview
from app.settings import settings
from app.services.store import SERVICE_MASTER_OPERATOR_ERROR, _schedule_status, store
from app.services.storage import object_storage
from app.services.testcase_verifier import UploadedTestcase, build_verified_testcase_set, verify_active_testcases_with_candidate_asset

router = APIRouter(tags=["operator"], route_class=ChangeNoticeRoute)


@router.get("/operator/contests/{contest_id}/mail-logs/{mail_id}/preview")
def operator_mail_preview(contest_id: str, mail_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.audit.view")
    return ok(request, mail_log_preview(mail_id, contest_id=contest_id))


@router.get("/operator/contests/{contest_id}/mail-logs")
async def operator_mail_logs(contest_id: str, request: Request, filters: dict = Depends(mail_log_filters)):
    require_contest_staff(request, contest_id, "contest.audit.view")
    if contest_id not in store.contests:
        raise not_found()
    logs, next_cursor, total = list_mail_logs(contest_id=contest_id, **filters)
    return page(request, logs, next_cursor=next_cursor, limit=filters["limit"], total_count=total, current_cursor=filters["cursor"])

@router.post("/operator/contests/{contest_id}/share-link")
def contest_share_link(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.notice.view")
    from app.services.contest_sharing import get_or_create
    return ok(request, {"code": get_or_create(contest_id)})


OPERATOR_TEST_TEAM_PREFIX = "__operator_test__"
from app.judge_languages import LANGUAGES, ProgrammingLanguages, validate_languages
SUPPORTED_JUDGE_LANGUAGES = set(LANGUAGES)


class ProblemLanguageResourceLimitPayload(BaseModel):
    disable_auto_adjustment: bool = Field(default=False, strict=True)
    time_limit_ms: int | None = Field(default=None, ge=100, le=10000)
    memory_limit_mb: int | None = Field(default=None, ge=16, le=1024)


def _clean_language_resource_limits(
    value: dict[str, ProblemLanguageResourceLimitPayload] | None,
) -> dict[str, dict[str, int]]:
    if not value:
        return {}

    cleaned: dict[str, dict[str, int]] = {}
    for language, limits in value.items():
        if language not in SUPPORTED_JUDGE_LANGUAGES:
            raise ValueError(f"unsupported language resource limit: {language}")
        payload = limits.model_dump(exclude_none=True)
        if not payload.get("disable_auto_adjustment"):
            payload.pop("disable_auto_adjustment", None)
        if payload:
            cleaned[language] = payload
    return cleaned


def _page_slice(items: list, limit: int, cursor: str | None) -> tuple[list, str | None]:
    safe_limit = max(1, min(limit, 300))
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    end = start + safe_limit
    next_cursor = str(end) if end < len(items) else None
    return items[start:end], next_cursor


def _submission_queue_position(contest_id: str, submission_id: str) -> int | None:
    return store.pending_queue_ranks(contest_id=contest_id).get(submission_id)


class TeamMemberPayload(BaseModel):
    name: str
    email: EmailStr


class ParticipantCreateRequest(BaseModel):
    team_name: str
    division_id: str
    leader: TeamMemberPayload
    members: list[TeamMemberPayload]


class ParticipantBulkCreateRequest(BaseModel):
    teams: list[ParticipantCreateRequest]


class ParticipantTeamUpdateRequest(BaseModel):
    team_name: str | None = None
    division_id: str | None = None
    status: str | None = None


class TeamMemberCreateRequest(BaseModel):
    name: str
    email: EmailStr
    role: TeamMemberRole = TeamMemberRole.MEMBER


class TeamMemberUpdateRequest(BaseModel):
    name: str | None = None
    email: EmailStr | None = None


class ContestSettingsUpdateRequest(BaseModel):
    title: str | None = None
    organization_name: str | None = None
    overview: str | None = None
    visibility: ContestVisibility | None = None
    visibility_after_end: ContestVisibility | None = None
    status: ContestStatus | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    freeze_at: datetime | None = None
    problem_access_after_end: ContestResourceAccess | None = None
    scoreboard_access_after_end: ContestResourceAccess | None = None
    submission_access_after_end: ContestResourceAccess | None = None
    board_access_after_end: ContestResourceAccess | None = None
    board_write_after_end: bool | None = None
    board_question_visibility_policy: Literal["choice", "public", "private"] | None = None
    board_question_default_visibility: Literal["public", "private"] | None = None
    board_comment_scope_policy: Literal["choice", "all", "operators"] | None = None
    board_comment_default_scope: Literal["all", "operators"] | None = None
    notice_access_after_end: ContestResourceAccess | None = None
    editorial_access_after_end: ContestResourceAccess | None = None
    scoreboard_freeze_mode: ScoreboardFreezeMode | None = None
    scoreboard_release_mode: ScoreboardReleaseMode | None = None
    mock_judging_enabled: bool | None = None
    participant_progress_visible: bool | None = None
    mock_judging_progress_visible: bool | None = None
    emergency_notice: str | None = None


class ContestNoticeCreateRequest(BaseModel):
    title: str
    body: str
    pinned: bool = False
    emergency: bool = False
    visibility: str = "public"


class ContestNoticeUpdateRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    pinned: bool | None = None
    emergency: bool | None = None
    visibility: str | None = None


class ContestAnswerCreateRequest(BaseModel):
    body: str
    visibility: str = "public"


class ContestAnswerUpdateRequest(BaseModel):
    visibility: str | None = None


class ContestQuestionUpdateRequest(BaseModel):
    visibility: str | None = None
    comment_scope: Literal["all", "operators"] | None = None


class OperatorTestSubmissionRequest(BaseModel):
    language: str
    source_code: str
    verification_asset_id: str | None = None


class DivisionCreateRequest(BaseModel):
    code: str | None = None
    name: str
    description: str = ""
    display_order: int | None = None


class DivisionUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    display_order: int | None = None


class ProblemCreateRequest(ProblemStatements, ProgrammingLanguages):
    division_id: str
    problem_code: str
    title: str
    statement: str
    time_limit_ms: int
    memory_limit_mb: int
    language_resource_limits: dict[str, ProblemLanguageResourceLimitPayload] = Field(default_factory=dict)
    display_order: int

    @model_validator(mode="after")
    def statement_languages(self):
        validate_statement_fields(self.default_statement_language, self.statement_translations, self.editorial_translations)
        return self

    @field_validator("language_resource_limits")
    @classmethod
    def validate_language_resource_limits(cls, value):
        _clean_language_resource_limits(value)
        return value


class ProblemCopyRequest(BaseModel):
    source_problem_id: str
    target_division_id: str
    problem_code: str | None = None
    display_order: int | None = None


class ProblemUpdateRequest(BaseModel):
    default_statement_language: str | None = Field(default=None, min_length=1, max_length=100)
    statement_translations: dict[str, StatementTranslation] | None = Field(default=None, max_length=20)
    editorial_translations: dict[str, str] | None = Field(default=None, max_length=20)
    division_id: str | None = None
    problem_code: str | None = None
    title: str | None = None
    statement: str | None = None
    editorial: str | None = None
    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None
    allowed_languages: list[str] | None = None

    @field_validator("allowed_languages", mode="before")
    @classmethod
    def languages(cls, value):
        return validate_languages(value)

    language_resource_limits: dict[str, ProblemLanguageResourceLimitPayload] | None = None
    display_order: int | None = None

    @field_validator("language_resource_limits")
    @classmethod
    def validate_language_resource_limits(cls, value):
        _clean_language_resource_limits(value)
        return value


class ProblemAssetCreateRequest(BaseModel):
    original_filename: str
    storage_key: str
    mime_type: str
    file_size: int
    sha256: str


class TestcaseSetCreateRequest(BaseModel):
    is_active: bool = False


class TestcaseSetUpdateRequest(BaseModel):
    is_active: bool | None = None


class TestcaseCreateRequest(BaseModel):
    display_order: int
    input_storage_key: str
    output_storage_key: str
    input_sha256: str
    output_sha256: str
    time_limit_ms_override: int | None = None
    memory_limit_mb_override: int | None = None


class VerifiedTestcasePayload(BaseModel):
    display_order: int
    input_storage_key: str
    output_storage_key: str
    input_sha256: str | None = None
    output_sha256: str | None = None


class VerifiedTestcaseSetCreateRequest(BaseModel):
    cases: list[VerifiedTestcasePayload]


class PresignUploadRequest(BaseModel):
    category: str
    filename: str
    content_type: str = "application/octet-stream"


def _problem_package_status(contest_id: str, problem_id: str) -> dict:
    assets = store.problem_assets_for_problem(contest_id, problem_id)
    testcase_sets = store.testcase_sets_for_problem(contest_id, problem_id)
    role_assets: dict[str, list] = {}
    for asset in assets:
        role = package_role(asset)
        if role:
            role_assets.setdefault(role, []).append(asset)
    active_set = next((item for item in testcase_sets if item.get("is_active")), None)
    active_count = len(active_set.get("testcases", [])) if active_set else 0
    required_roles = [
        ("package-resource", "testlib.h", True),
        ("validator", "validator.cpp", True),
        ("checker", "checker.cpp", False),
    ]
    support_files = []
    warnings = []
    for role, label, required in required_roles:
        files = sorted(role_assets.get(role, []), key=lambda item: item.created_at)
        if required and not files:
            warnings.append(f"{label} 파일이 없습니다.")
        support_files.append(
            {
                "role": role,
                "label": label,
                "required": required,
                "count": len(files),
                "latest_filename": files[-1].original_filename if files else None,
                "status": "ready" if files else "missing",
            }
        )
    if not active_set:
        warnings.append("활성 테스트케이스 세트가 없습니다.")
    elif active_count <= 0:
        warnings.append(f"활성 테스트케이스 세트 v{active_set.get('version')}에 케이스가 없습니다.")
    return {
        "ready": not warnings,
        "warnings": warnings,
        "support_files": support_files,
        "active_testcase_set": active_set,
        "active_testcase_count": active_count,
        "testcase_set_count": len(testcase_sets),
        "judge_bundle": store.problem_judge_bundle_status(contest_id, problem_id),
    }


def _testcase_pairs_from_zip(archive: bytes) -> list[tuple[str, bytes, bytes]]:
    try:
        zip_file = zipfile.ZipFile(BytesIO(archive))
    except zipfile.BadZipFile as error:
        raise AppError(422, "invalid_archive", "zip 파일을 열 수 없습니다.") from error
    pairs: dict[str, dict[str, bytes]] = {}
    for info in zip_file.infolist():
        if info.is_dir():
            continue
        path = Path(info.filename)
        filename = path.name
        if filename.startswith(".") or "__MACOSX" in path.parts:
            continue
        suffix = path.suffix.lower()
        if suffix not in {".in", ".out"}:
            continue
        stem = str(path.with_suffix("")).replace("\\", "/")
        if info.file_size > 16 * 1024 * 1024:
            raise AppError(422, "archive_file_too_large", f"{info.filename} 파일이 너무 큽니다.")
        pairs.setdefault(stem, {})[suffix[1:]] = zip_file.read(info)
    result = []
    missing = []
    for stem in sorted(pairs):
        pair = pairs[stem]
        if "in" not in pair or "out" not in pair:
            missing.append(stem)
            continue
        result.append((stem, pair["in"], pair["out"]))
    if missing:
        raise AppError(422, "testcase_pair_missing", f".in/.out 쌍이 맞지 않습니다: {', '.join(missing[:5])}")
    if not result:
        raise AppError(422, "testcase_pair_missing", "zip 안에 .in/.out 테스트케이스 쌍이 없습니다.")
    return result


def _division_code_from_name(name: str) -> str:
    code = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return code or "division"


def _require_contest_mutation_open(contest_id: str):
    contest = store.get_contest(contest_id)
    if not contest:
        raise not_found()
    if allows_live_problem_change(contest_id):
        return contest
    now = now_utc()
    in_time_window = contest.status not in {ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD} and contest.start_at <= now < contest.end_at and contest.status not in {
        ContestStatus.ENDED,
        ContestStatus.FINALIZED,
        ContestStatus.ARCHIVED,
    }
    if contest.status == ContestStatus.RUNNING or in_time_window:
        raise AppError(409, "contest_locked", "Contest is in progress. Operation-changing updates are locked during contest time.")
    return contest


def _settings_update_changes_operation(contest, updates: dict) -> bool:
    operation_locked_fields = {
        "title",
        "organization_name",
        "overview",
        "status",
    }
    for key in operation_locked_fields:
        if key not in updates:
            continue
        current_value = getattr(contest, key)
        next_value = updates[key]
        if isinstance(current_value, ContestStatus):
            current_value = current_value.value
        if isinstance(next_value, ContestStatus):
            next_value = next_value.value
        if current_value != next_value:
            return True
    return False


def _schedule_bundle_warm(background_tasks: BackgroundTasks, contest_id: str, problem_id: str) -> None:
    store.enqueue_bundle_warm(contest_id, problem_id)


def _require_storage_namespace(contest_id: str, *keys: str) -> None:
    for key in keys:
        try:
            object_storage.validate_key(key)
        except ValueError:
            raise AppError(422, "invalid_storage_key", "Invalid storage key.")
        if not key.startswith(f"contests/{contest_id}/"):
            raise permission_denied("Storage objects must belong to this contest.")


def _contest_staff_payload(account, contest_id: str) -> dict:
    payload = account.model_dump(mode="json")
    payload["contest_scopes"] = {contest_id: account.contest_scopes.get(contest_id, [])}
    payload["contest_roles"] = {contest_id: account.contest_roles.get(contest_id, [])}
    payload["protected_master_contests"] = [contest_id] if contest_id in account.protected_master_contests else []
    return payload


def _check_staff_assignment(actor, contest_id: str, email: str, roles: list[str] | None) -> None:
    target = next((item for item in store.contest_operator_accounts(contest_id) if str(item.email).lower() == email.strip().lower()), None)
    target_master = target and is_contest_master(target, contest_id)
    if (target_master or roles == ["master"]) and not is_contest_master(actor, contest_id):
        raise permission_denied("대회 마스터 권한은 대회 마스터만 변경할 수 있습니다.")
    if target and contest_id in target.protected_master_contests and roles != ["master"]:
        raise AppError(409, "contest_owner_immutable", "대회 총괄은 강등하거나 제거할 수 없습니다. 먼저 다른 운영자에게 총괄을 위임해 주세요.")
    if target_master and roles != ["master"]:
        masters = [item for item in store.contest_operator_accounts(contest_id) if is_contest_master(item, contest_id)]
        if len(masters) <= 1:
            raise AppError(409, "last_contest_master", "마지막 대회 마스터는 변경하거나 제거할 수 없습니다.")


def _check_test_submission_access(account, contest_id: str, submission) -> None:
    if not submission or submission.contest_id != contest_id or submission.submission_kind != "operator_test":
        raise not_found()
    if not has_contest_permission(account, contest_id, "contest.submission.view") and str(submission.submitted_by_email or "").lower() != str(account.email).lower():
        raise permission_denied()


def _test_submission_payload(account, contest_id: str, submission) -> dict:
    payload = submission.model_dump(mode="json")
    if not has_contest_permission(account, contest_id, "contest.problem.resource.view") and not has_contest_permission(account, contest_id, "contest.submission.view"):
        # Judge diagnostics contain private inputs, reference outputs and checker paths.
        payload["judge_message"] = None
    return payload


@router.get("/operator/contests")
async def operator_contests(request: Request):
    account = require_staff(request)
    contests = store.accessible_contests_for_staff(account)
    return page(request, [contest.model_dump(mode="json") for contest in contests])


@router.get("/operator/contests/{contest_id}/dashboard")
async def operator_dashboard(contest_id: str, request: Request):
    account = require_contest_staff(request, contest_id)
    contest = store.contests.get(contest_id)
    if not contest:
        raise not_found()
    participant_teams = [
        team
        for team in store.teams.values()
        if team.contest_id == contest_id and not team.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX)
    ]
    return ok(
        request,
        {
            "contest": contest.model_dump(mode="json"),
            "divisions": [division.model_dump(mode="json") for division in store.contest_divisions(contest_id)],
            "participant_count": len(participant_teams) if has_contest_permission(account, contest_id, "contest.participant.view") else 0,
            "submission_count": store.count_submissions(contest_id=contest_id) if has_contest_permission(account, contest_id, "contest.submission.view") else 0,
            "pending_jobs": store.count_judge_jobs(contest_id=contest_id, status="pending") if has_contest_permission(account, contest_id, "contest.submission.view") else 0,
            "operators": [_contest_staff_payload(item, contest_id) for item in store.contest_operator_accounts(contest_id)] if has_contest_permission(account, contest_id, "contest.staff.manage") else [],
            "participant_count_by_division": {
                division.division_id: len(
                    [team for team in participant_teams if team.division_id == division.division_id]
                )
                for division in store.contest_divisions(contest_id)
            } if has_contest_permission(account, contest_id, "contest.participant.view") else {},
        },
    )


@router.get("/operator/contests/{contest_id}/audit-logs")
def operator_audit_logs(
    contest_id: str,
    request: Request,
    actor_email: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
):
    require_contest_staff(request, contest_id, "contest.audit.view")
    if contest_id not in store.contests:
        raise not_found()
    logs, next_cursor, total_count = store.list_operational_audit_logs(
        contest_id=contest_id,
        actor_email=actor_email or None,
        limit=limit,
        cursor=cursor,
    )
    return page(
        request,
        [item.model_dump(mode="json") for item in logs],
        next_cursor=next_cursor,
        limit=max(1, min(limit, 300)),
        total_count=total_count,
        current_cursor=cursor,
    )


@router.get("/operator/contests/{contest_id}/access-logs")
def operator_access_logs(
    contest_id: str,
    request: Request,
    email: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
):
    require_contest_staff(request, contest_id, "contest.access_log.view")
    if contest_id not in store.contests:
        raise not_found()
    logs, next_cursor, total_count = store.list_access_logs(
        contest_id=contest_id,
        email=email or None,
        limit=limit,
        cursor=cursor,
    )
    return page(
        request,
        [item.model_dump(mode="json") for item in logs],
        next_cursor=next_cursor,
        limit=max(1, min(limit, 300)),
        total_count=total_count,
        current_cursor=cursor,
    )


@router.get("/operator/contests/{contest_id}/access-log-stats")
def operator_access_log_stats(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.access_log.view")
    if contest_id not in store.contests:
        raise not_found()
    return ok(request, store.access_log_stats(contest_id=contest_id))


@router.get("/operator/contests/{contest_id}/divisions")
async def divisions(contest_id: str, request: Request):
    require_contest_staff(request, contest_id)
    if contest_id not in store.contests:
        raise not_found()
    return page(request, [division.model_dump(mode="json") for division in store.contest_divisions(contest_id)])


@router.post("/operator/contests/{contest_id}/divisions")
async def create_division(contest_id: str, payload: DivisionCreateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    _require_contest_mutation_open(contest_id)
    try:
        division = store.create_contest_division(
            contest_id=contest_id,
            code=payload.code or _division_code_from_name(payload.name),
            name=payload.name,
            description=payload.description,
            display_order=payload.display_order or 1,
        )
    except ValueError as error:
        message = str(error)
        if message == "contest not found":
            raise not_found()
        raise AppError(422, "validation_error", message)
    return ok(request, division.model_dump(mode="json"))


@router.patch("/operator/contests/{contest_id}/divisions/{division_id}")
async def update_division(contest_id: str, division_id: str, payload: DivisionUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    _require_contest_mutation_open(contest_id)
    try:
        division = store.update_contest_division(contest_id, division_id, **payload.model_dump(exclude_unset=True))
    except ValueError as error:
        raise AppError(422, "validation_error", str(error))
    if not division:
        raise not_found()
    return ok(request, division.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/divisions/{division_id}")
async def delete_division(contest_id: str, division_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    _require_contest_mutation_open(contest_id)
    if not store.delete_contest_division(contest_id, division_id):
        raise not_found()
    return ok(request, {"deleted": True})


@router.patch("/operator/contests/{contest_id}/settings")
async def update_contest_settings(contest_id: str, payload: ContestSettingsUpdateRequest, request: Request):
    account = require_contest_staff(request, contest_id)
    contest = store.get_contest(contest_id)
    if not contest:
        raise not_found()
    updates = payload.model_dump(exclude_unset=True)
    for field in updates:
        permission = (
            "contest.scoreboard.manage" if field == "scoreboard_freeze_mode"
            else "contest.notice.manage" if field == "emergency_notice"
            else "contest.settings.manage"
        )
        if not has_contest_permission(account, contest_id, permission):
            raise permission_denied()
    if _settings_update_changes_operation(contest, updates):
        _require_contest_mutation_open(contest_id)

    next_problem_access = updates.get("problem_access_after_end", contest.problem_access_after_end)
    if next_problem_access == ContestResourceAccess.PRIVATE:
        updates["editorial_access_after_end"] = ContestResourceAccess.PRIVATE
        updates["mock_judging_enabled"] = False
    next_participant_progress_visible = updates.get(
        "participant_progress_visible",
        contest.participant_progress_visible,
    )
    if next_participant_progress_visible:
        updates["mock_judging_progress_visible"] = False

    explicit_start = "start_at" in updates
    explicit_end = "end_at" in updates
    explicit_freeze = "freeze_at" in updates

    start_at = updates.get("start_at", contest.start_at)
    end_at = updates.get("end_at", contest.end_at)
    freeze_at = updates.get("freeze_at", contest.freeze_at)

    # Allow practical schedule edits while contest is running/ended.
    # If only one side of time range is edited and range becomes invalid,
    # keep existing duration when possible instead of hard-failing.
    if start_at >= end_at:
        current_duration = max(contest.end_at - contest.start_at, timedelta(hours=1))
        if explicit_start and not explicit_end:
            end_at = start_at + current_duration
            updates["end_at"] = end_at
        elif explicit_end and not explicit_start:
            start_at = end_at - current_duration
            updates["start_at"] = start_at
        else:
            raise AppError(422, "validation_error", "start_at must be before end_at.")

    if not (start_at <= freeze_at <= end_at):
        if explicit_freeze:
            raise AppError(422, "validation_error", "freeze_at must be between start_at and end_at.")
        # If freeze wasn't explicitly edited, auto-clamp to a sane point.
        default_freeze = end_at - timedelta(hours=1)
        if default_freeze < start_at:
            default_freeze = start_at
        if default_freeze > end_at:
            default_freeze = end_at
        freeze_at = default_freeze
        updates["freeze_at"] = freeze_at
    time_fields = ("start_at", "freeze_at", "end_at")
    changed_time_fields = [
        (key, getattr(contest, key), updates[key])
        for key in time_fields
        if key in updates and getattr(contest, key) != updates[key]
    ]
    auto_notice = time_update_notice_body(contest.title, changed_time_fields)
    current_status = _schedule_status(contest.status, contest.start_at, contest.end_at, now_utc())
    next_status = _schedule_status(updates.get("status") or contest.status, start_at, end_at, now_utc())
    announce_time_change = (
        bool(auto_notice)
        and (current_status in {ContestStatus.OPEN, ContestStatus.RUNNING}
             or next_status in {ContestStatus.OPEN, ContestStatus.RUNNING})
    )
    if announce_time_change:
        await confirm_change(request, contest, "대회 일정 변경", auto_notice, contest.model_dump(mode="json"))
        updates["emergency_notice"] = request.state.change_notice[2]
    try:
        updated = store.update_contest_settings(contest_id, **updates)
    except ValueError as error:
        raise AppError(409, "scoreboard_release_conflict", str(error))
    if not updated:
        raise not_found()
    if not store._contest_accepts_participant_invites(contest.status.value) and store._contest_accepts_participant_invites(updated.status.value):
        store.enqueue_participant_invites_for_contest(contest_id)
    return ok(request, updated.model_dump(mode="json"))


@router.get("/operator/contests/{contest_id}/operators")
async def contest_operators(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.staff.manage")
    if contest_id not in store.contests:
        raise not_found()
    return page(request, [_contest_staff_payload(account, contest_id) for account in store.contest_operator_accounts(contest_id)])


class ContestOwnerTransferRequest(BaseModel):
    email: EmailStr


@router.post("/operator/contests/{contest_id}/owner:transfer")
async def transfer_contest_owner(contest_id: str, payload: ContestOwnerTransferRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.staff.manage")
    changed = store.transfer_contest_owner(contest_id, str(payload.email), account)
    return ok(request, [_contest_staff_payload(item, contest_id) for item in changed])


@router.post("/operator/contests/{contest_id}/operators")
async def create_contest_operator(contest_id: str, payload: ContestOperatorCreateRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.staff.manage")
    _check_staff_assignment(account, contest_id, str(payload.email), payload.roles)
    try:
        operator = store.upsert_contest_operator(contest_id, str(payload.email), payload.display_name, payload.roles, actor=account)
    except ValueError as exc:
        message = str(exc)
        if message == SERVICE_MASTER_OPERATOR_ERROR:
            raise AppError(409, "service_master_operator_immutable", "서비스 마스터는 모든 대회 권한을 자동으로 가지므로 대회 운영자로 추가할 수 없습니다.")
        if message.startswith("operator email cannot be participant email:"):
            raise AppError(422, "validation_error", message, {"field": "operator_email_conflict"})
        raise AppError(404, "not_found", message)
    _enqueue_operator_assignment_mail(contest_id, operator, payload.roles)
    return ok(request, _contest_staff_payload(operator, contest_id))


def _enqueue_operator_assignment_mail(contest_id, operator, fallback_roles):
    contest = store.contests.get(contest_id)
    if contest:
        roles = operator.contest_roles.get(contest_id, fallback_roles)
        console_path = f"/operator/contests/{contest_id}"
        content = operator_assignment_mail(
            contest_title=contest.title,
            organization_name=contest.organization_name,
            display_name=operator.display_name,
            role_label=title_for_roles(roles) or "운영자",
            starts_at=None if contest.status in {ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD} else contest.start_at,
            console_url=absolute_url(console_path),
        )
        store.enqueue_mail(
            "contest_operator_assigned",
            str(operator.email),
            content.subject,
            content.body_text,
            content.body_html,
            contest_id=contest_id,
        )


@router.patch("/operator/contests/{contest_id}/operators/{operator_email}")
async def update_contest_operator(contest_id: str, operator_email: str, payload: ContestOperatorUpdateRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.staff.manage")
    _check_staff_assignment(account, contest_id, operator_email, payload.roles)
    operator = store.update_contest_operator(contest_id, operator_email, payload.display_name, payload.roles, new_email=str(payload.email) if payload.email is not None else None, actor=account)
    if not operator:
        raise not_found()
    return ok(request, _contest_staff_payload(operator, contest_id))


@router.post("/operator/contests/{contest_id}/operators/{operator_email}/account:replace")
async def replace_contest_operator_account(contest_id: str, operator_email: str, payload: ContestOperatorCreateRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.staff.manage")
    _check_staff_assignment(account, contest_id, operator_email, payload.roles)
    operator = store.replace_contest_operator_account(contest_id, operator_email, str(payload.email), payload.display_name, payload.roles, actor=account)
    _enqueue_operator_assignment_mail(contest_id, operator, payload.roles)
    return ok(request, _contest_staff_payload(operator, contest_id))


@router.delete("/operator/contests/{contest_id}/operators/{operator_email}")
async def delete_contest_operator(contest_id: str, operator_email: str, request: Request):
    account = require_contest_staff(request, contest_id, "contest.staff.manage")
    _check_staff_assignment(account, contest_id, operator_email, None)
    current_operators = store.contest_operator_accounts(contest_id)
    normalized_email = operator_email.strip().lower()
    if store.is_service_master_email(operator_email):
        raise AppError(409, "service_master_operator_immutable", "서비스 마스터는 대회 운영자 목록에서 제거할 수 없습니다.")
    if normalized_email not in {str(operator.email).lower() for operator in current_operators}:
        raise not_found()
    if str(account.email).lower() == normalized_email:
        raise AppError(409, "self_remove_denied", "현재 로그인한 운영자 자신은 제거할 수 없습니다.")
    if len(current_operators) <= 1:
        raise AppError(409, "last_operator", "마지막 대회 운영자는 제거할 수 없습니다.")
    removed = store.remove_contest_operator(contest_id, operator_email, actor=account)
    if not removed:
        raise not_found()
    return ok(request, _contest_staff_payload(removed, contest_id))


@router.get("/operator/contests/{contest_id}/notices")
async def operator_notices(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.notice.view")
    if contest_id not in store.contests:
        raise not_found()
    return page(request, [notice.model_dump(mode="json") for notice in store.contest_notices_for_view(contest_id, operator=True)])


@router.post("/operator/contests/{contest_id}/notices")
async def create_notice(contest_id: str, payload: ContestNoticeCreateRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.notice.manage")
    if payload.visibility not in {"public", "participants"}:
        raise AppError(422, "validation_error", "Unsupported notice visibility.")
    notice = store.create_contest_notice(
        contest_id,
        payload.title,
        payload.body,
        payload.pinned,
        payload.emergency,
        payload.visibility,
        str(account.email),
    )
    return ok(request, notice.model_dump(mode="json"))


@router.patch("/operator/contests/{contest_id}/notices/{notice_id}")
async def update_notice(contest_id: str, notice_id: str, payload: ContestNoticeUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.notice.manage")
    updates = payload.model_dump(exclude_unset=True)
    if "visibility" in updates and updates["visibility"] not in {"public", "participants"}:
        raise AppError(422, "validation_error", "Unsupported notice visibility.")
    notice = store.update_contest_notice(contest_id, notice_id, **updates)
    if not notice:
        raise not_found()
    return ok(request, notice.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/notices/{notice_id}")
async def delete_notice(contest_id: str, notice_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.notice.manage")
    deleted = store.delete_contest_notice(contest_id, notice_id)
    if not deleted:
        raise not_found()
    return ok(request, {"contest_notice_id": notice_id, "deleted": True})


@router.get("/operator/contests/{contest_id}/boards")
async def operator_board(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.board.question.view")
    if contest_id not in store.contests:
        raise not_found()
    return page(request, [question.model_dump(mode="json") for question in store.questions_for_view(contest_id, operator=True)])


@router.patch("/operator/contests/{contest_id}/boards/{question_id}")
async def update_question(contest_id: str, question_id: str, payload: ContestQuestionUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.board.question.manage")
    updates = payload.model_dump(exclude_unset=True)
    if "visibility" in updates and updates["visibility"] not in {"public", "private"}:
        raise AppError(422, "validation_error", "Unsupported question visibility.")
    question = store.update_question(contest_id, question_id, **updates)
    if not question:
        raise not_found()
    return ok(request, question.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/boards/{question_id}")
async def delete_question(contest_id: str, question_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.board.question.manage")
    deleted = store.delete_question(contest_id, question_id)
    if not deleted:
        raise not_found()
    return ok(request, {"contest_question_id": question_id, "deleted": True})


@router.post("/operator/contests/{contest_id}/boards/{question_id}/answers")
async def create_answer(contest_id: str, question_id: str, payload: ContestAnswerCreateRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.board.question.manage")
    if payload.visibility not in {"public", "questioner"}:
        raise AppError(422, "validation_error", "Unsupported answer visibility.")
    answer = store.create_answer(contest_id, question_id, payload.body, payload.visibility, str(account.email))
    if not answer:
        raise not_found()
    question = store.get_contest_question(contest_id, question_id)
    contest = store.contests.get(contest_id)
    if question and contest:
        recipient_emails = store.participant_team_member_emails(contest_id, question.participant_team_id)
        subject = f"[ZOJ] 질문 답변 등록 · {contest.title}"
        question_url = absolute_url(f"/contests/{contest_id}/board?questionId={question.contest_question_id}")
        visibility_label = "전체 공개" if answer.visibility == "public" else "질문자에게만 공개"
        body_lines = [
            f"대회: {contest.title}",
            f"질문 제목: {question.title}",
            f"답변 공개 범위: {visibility_label}",
            f"답변자: {account.display_name} <{account.email}>",
            "",
            "답변 본문:",
            answer.body,
            "",
            f"바로가기: {question_url}",
        ]
        for email in recipient_emails:
            store.enqueue_mail(
                "contest_question_answered",
                email,
                subject,
                "\n".join(body_lines),
                render_branded_email(
                    title="질문에 답변이 등록되었습니다",
                    eyebrow="질문 답변",
                    preheader=question.title,
                    body=[f"{contest.title} 질문에 답변이 등록되었습니다."],
                    meta=[
                        ("대회", contest.title),
                        ("질문 제목", question.title),
                        ("공개 범위", visibility_label),
                        ("답변자", f"{account.display_name} <{account.email}>"),
                    ],
                    sections=[("답변 본문", answer.body), ("질문 본문", question.body)],
                    button_label="답변 확인하기",
                    button_url=question_url,
                ),
                contest_id=contest_id,
            )
    return ok(request, answer.model_dump(mode="json"))


@router.patch("/operator/contests/{contest_id}/boards/{question_id}/answers/{answer_id}")
async def update_answer(contest_id: str, question_id: str, answer_id: str, payload: ContestAnswerUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.board.question.manage")
    updates = payload.model_dump(exclude_unset=True)
    if "visibility" in updates and updates["visibility"] not in {"public", "questioner"}:
        raise AppError(422, "validation_error", "Unsupported answer visibility.")
    answer = store.update_answer(contest_id, question_id, answer_id, **updates)
    if not answer:
        raise not_found()
    return ok(request, answer.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/boards/{question_id}/answers/{answer_id}")
async def delete_answer(contest_id: str, question_id: str, answer_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.board.question.manage")
    deleted = store.delete_answer(contest_id, question_id, answer_id)
    if not deleted:
        raise not_found()
    return ok(request, {"contest_answer_id": answer_id, "deleted": True})


@router.get("/operator/contests/{contest_id}/participants")
async def participants(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.view")
    items = []
    for team in store.participant_teams_for_operator(contest_id):
        division = store.get_division(contest_id, team.division_id)
        items.append({**team.model_dump(mode="json"), "division": division.model_dump(mode="json") if division else None})
    return page(request, items)


@router.post("/operator/contests/{contest_id}/participants")
async def create_participant(contest_id: str, payload: ParticipantCreateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    if not store.get_division(contest_id, payload.division_id):
        raise not_found("Contest division is not configured.")
    try:
        team = store.create_participant_team(
            contest_id=contest_id,
            division_id=payload.division_id,
            team_name=payload.team_name,
            leader_name=payload.leader.name,
            leader_email=str(payload.leader.email),
            members=[(member.name, str(member.email)) for member in payload.members],
        )
    except ValueError as error:
        raise AppError(422, "validation_error", str(error), {"field": "email_conflict"})
    store.enqueue_participant_invite_for_team(contest_id, team.participant_team_id)
    return ok(request, team.model_dump(mode="json"))


@router.post("/operator/contests/{contest_id}/participants:bulk-create")
async def bulk_create_participants(contest_id: str, payload: ParticipantBulkCreateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    created = []
    errors = []
    for index, item in enumerate(payload.teams, start=1):
        if not store.get_division(contest_id, item.division_id):
            errors.append({"row": index, "team_name": item.team_name, "message": "Contest division is not configured."})
            continue
        try:
            team = store.create_participant_team(
                contest_id=contest_id,
                division_id=item.division_id,
                team_name=item.team_name,
                leader_name=item.leader.name,
                leader_email=str(item.leader.email),
                members=[(member.name, str(member.email)) for member in item.members],
            )
            store.enqueue_participant_invite_for_team(contest_id, team.participant_team_id)
            division = store.get_division(contest_id, team.division_id)
            created.append({**team.model_dump(mode="json"), "division": division.model_dump(mode="json") if division else None})
        except ValueError as error:
            message = str(error)
            if message.startswith("participant email already registered:"):
                message = "participant email already registered"
            errors.append({"row": index, "team_name": item.team_name, "message": message})
    return ok(request, {"created": created, "errors": errors})


@router.patch("/operator/contests/{contest_id}/participants/{participant_team_id}")
async def update_participant(contest_id: str, participant_team_id: str, payload: ParticipantTeamUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    if payload.status is not None and payload.status not in {"invited", "active", "disabled", "disqualified"}:
        raise AppError(422, "validation_error", "Unsupported participant team status.")
    if payload.team_name is not None or payload.division_id is not None or payload.status is not None:
        _require_contest_mutation_open(contest_id)
    try:
        team = store.update_participant_team(
            contest_id=contest_id,
            participant_team_id=participant_team_id,
            team_name=payload.team_name,
            division_id=payload.division_id,
            status=payload.status,
        )
    except ValueError:
        raise not_found("Contest division is not configured.")
    if not team:
        raise not_found()
    division = store.get_division(contest_id, team.division_id)
    return ok(request, {**team.model_dump(mode="json"), "division": division.model_dump(mode="json") if division else None})


@router.delete("/operator/contests/{contest_id}/participants/{participant_team_id}")
async def delete_participant(contest_id: str, participant_team_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    deleted, reason = store.delete_participant_team(contest_id, participant_team_id)
    if not deleted:
        if reason == "has_submission":
            raise AppError(409, "participant_has_submission", "제출 이력이 있는 참가팀은 삭제할 수 없습니다.")
        if reason == "has_question":
            raise AppError(409, "participant_has_question", "질문 이력이 있는 참가팀은 삭제할 수 없습니다.")
        raise not_found()
    return ok(request, {"participant_team_id": participant_team_id, "deleted": True})


@router.post("/operator/contests/{contest_id}/participants/{participant_team_id}/members")
async def add_participant_member(contest_id: str, participant_team_id: str, payload: TeamMemberCreateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    try:
        member = store.add_team_member(
            contest_id=contest_id,
            participant_team_id=participant_team_id,
            name=payload.name,
            email=str(payload.email),
            role=payload.role,
        )
    except ValueError as error:
        raise AppError(422, "validation_error", str(error), {"field": "email_conflict"})
    if not member:
        raise not_found()
    return ok(request, member.model_dump(mode="json"))


@router.patch("/operator/contests/{contest_id}/participants/{participant_team_id}/members/{team_member_id}")
async def update_participant_member(contest_id: str, participant_team_id: str, team_member_id: str, payload: TeamMemberUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.participant.manage")
    try:
        member = store.update_team_member(
            contest_id=contest_id,
            participant_team_id=participant_team_id,
            team_member_id=team_member_id,
            name=payload.name,
            email=str(payload.email) if payload.email is not None else None,
        )
    except ValueError as error:
        raise AppError(422, "validation_error", str(error), {"field": "email_conflict"})
    if not member:
        raise not_found()
    return ok(request, member.model_dump(mode="json"))


@router.post("/operator/contests/{contest_id}/participants/{participant_team_id}/members/{team_member_id}/sessions:revoke")
async def revoke_participant_member_sessions(contest_id: str, participant_team_id: str, team_member_id: str, request: Request):
    operator = require_contest_staff(request, contest_id, "contest.participant.manage")
    member = store.revoke_team_member_sessions(contest_id, participant_team_id, team_member_id)
    if not member:
        raise not_found()
    team = store.teams.get(participant_team_id)
    write_access_log(
        request, event_type="account_revoked", account_scope="participant",
        email=str(member.email), display_name=member.name, contest_id=contest_id,
        participant_team_id=participant_team_id, team_name=team.team_name if team else None,
        team_member_id=team_member_id, member_name=member.name, actor_role="participant",
        details={"revoked_by": str(operator.email), "scope": "account"},
    )
    return ok(request, member.model_dump(mode="json"))


@router.get("/operator/contests/{contest_id}/submission-filters")
async def submission_filters(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.submission.view")
    return ok(request, {
        "problems": [{key: getattr(problem, key) for key in ("problem_id", "problem_code", "title", "division_id")} for problem in store.problems.values() if problem.contest_id == contest_id],
        "teams": [{key: getattr(team, key) for key in ("participant_team_id", "team_name", "division_id")} for team in store.participant_teams_for_operator(contest_id)],
    })


@router.get("/operator/contests/{contest_id}/submissions")
async def operator_submissions(
    contest_id: str,
    request: Request,
    limit: int = 100,
    cursor: str | None = None,
    include_source: bool = False,
    division_id: str | None = None,
    problem_id: str | None = None,
    participant_team_id: str | None = None,
    submission_kind: Literal["participant", "operator_test", "participant_preview", "mock_judging"] | None = None,
):
    require_contest_staff(request, contest_id, "contest.submission.view")
    submissions, next_cursor, total_count = store.list_submissions(
        contest_id=contest_id,
        division_id=division_id,
        problem_id=problem_id,
        participant_team_id=participant_team_id,
        include_source=include_source,
        include_participant_previews=True,
        submission_kind=submission_kind,
        limit=limit,
        cursor=cursor,
    )
    teams = store.teams_by_ids([submission.participant_team_id for submission in submissions])
    members = {
        member.team_member_id: member
        for team in teams.values()
        for member in team.members
    }
    queue_rank_by_submission_id = store.pending_queue_ranks(contest_id=contest_id)
    source_lengths = store.submission_source_lengths([submission.submission_id for submission in submissions])
    items = []
    for submission in submissions:
        payload = submission.model_dump(mode="json")
        team = teams.get(submission.participant_team_id)
        member = members.get(submission.team_member_id)
        payload["team_name"] = team.team_name if team else None
        payload["member_name"] = member.name if member else None
        payload["member_email"] = str(member.email) if member else None
        payload["queue_position"] = queue_rank_by_submission_id.get(submission.submission_id)
        payload["source_code_length"] = source_lengths.get(submission.submission_id, 0)
        if not include_source:
            payload["source_code"] = None
        items.append(payload)
    return page(
        request,
        items,
        next_cursor=next_cursor,
        limit=max(1, min(limit, 300)),
        total_count=total_count,
        current_cursor=cursor,
    )


@router.get("/operator/contests/{contest_id}/submissions/{submission_id}")
async def operator_submission_detail(contest_id: str, submission_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.submission.view")
    submission = store.get_submission(submission_id)
    if not submission or submission.contest_id != contest_id:
        raise not_found()
    team = store.teams_by_ids([submission.participant_team_id]).get(submission.participant_team_id)
    member = next((item for item in (team.members if team else []) if item.team_member_id == submission.team_member_id), None)
    payload = submission.model_dump(mode="json")
    payload["team_name"] = team.team_name if team else None
    payload["member_name"] = member.name if member else None
    payload["member_email"] = str(member.email) if member else None
    payload["queue_position"] = _submission_queue_position(contest_id, submission_id)
    payload["source_code_length"] = len((submission.source_code or "").encode("utf-8"))
    return ok(request, payload)


@router.post("/operator/contests/{contest_id}/submissions/{submission_id}/rejudge")
async def retry_system_error(contest_id: str, submission_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.submission.view")
    require_contest_staff(request, contest_id, "contest.problem.test")
    submission, previous = store.retry_system_error_submission(contest_id, submission_id)
    request.state.audit_rejudge = {
        "submission_id": submission_id,
        "problem_id": submission.problem_id,
        "previous_result": previous,
        "new_status": "waiting",
        "original_submitted_at": submission.submitted_at.isoformat(),
    }
    return ok(request, {**submission.model_dump(mode="json"),
        "queue_position": _submission_queue_position(contest_id, submission_id)})


@router.get("/operator/contests/{contest_id}/submissions/{submission_id}/status:wait")
async def operator_wait_submission_status(
    contest_id: str,
    submission_id: str,
    request: Request,
    wait_seconds: float = 2.0,
    poll_interval_seconds: float = 0.25,
):
    require_contest_staff(request, contest_id, "contest.submission.view")
    submission = store.get_submission(submission_id, include_source=False)
    if not submission or submission.contest_id != contest_id:
        raise not_found()
    wait_budget = max(0.0, min(wait_seconds, 10.0))
    poll = max(0.1, min(poll_interval_seconds, 1.0))
    loops = max(1, int(wait_budget / poll))
    for _ in range(loops):
        updated = store.get_submission(submission_id, include_source=False)
        if not updated:
            raise not_found()
        if updated.status not in {"waiting", "preparing", "judging"}:
            payload = updated.model_dump(mode="json")
            payload["queue_position"] = _submission_queue_position(contest_id, submission_id)
            payload["source_code"] = None
            return ok(request, payload)
        await asyncio.sleep(poll)
    latest = store.get_submission(submission_id, include_source=False)
    if not latest:
        raise not_found()
    payload = latest.model_dump(mode="json")
    payload["queue_position"] = _submission_queue_position(contest_id, submission_id)
    payload["source_code"] = None
    return ok(request, payload)


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/test-submissions")
async def create_operator_test_submission(contest_id: str, problem_id: str, payload: OperatorTestSubmissionRequest, request: Request):
    account = require_contest_staff(request, contest_id, "contest.problem.test")
    if payload.verification_asset_id:
        require_contest_staff(request, contest_id, "contest.problem.resource.view")
    if payload.language not in set(LANGUAGES):
        raise AppError(422, "validation_error", "Unsupported language.", {"fields": [{"path": "body.language", "code": "invalid_enum"}]})
    if not payload.source_code.strip():
        raise AppError(422, "validation_error", "Source code is required.")
    try:
        submission = store.create_operator_test_submission(
            contest_id,
            problem_id,
            payload.language,
            payload.source_code,
            verification_asset_id=payload.verification_asset_id,
            submitted_by_name=account.display_name,
            submitted_by_email=str(account.email),
        )
    except ValueError:
        raise not_found()
    return ok(request, submission.model_dump(mode="json"))


@router.get("/operator/contests/{contest_id}/test-submissions/{submission_id}")
async def operator_test_submission_detail(contest_id: str, submission_id: str, request: Request):
    account = require_contest_staff(request, contest_id, "contest.problem.test", "contest.submission.view")
    submission = store.get_submission(submission_id)
    _check_test_submission_access(account, contest_id, submission)
    return ok(request, _test_submission_payload(account, contest_id, submission))


@router.get("/operator/contests/{contest_id}/test-submissions/{submission_id}/status:wait")
async def operator_wait_test_submission_status(
    contest_id: str,
    submission_id: str,
    request: Request,
    wait_seconds: float = 2.0,
    poll_interval_seconds: float = 0.25,
):
    account = require_contest_staff(request, contest_id, "contest.problem.test", "contest.submission.view")
    submission = store.get_submission(submission_id)
    _check_test_submission_access(account, contest_id, submission)
    wait_budget = max(0.0, min(wait_seconds, 10.0))
    poll = max(0.1, min(poll_interval_seconds, 1.0))
    loops = max(1, int(wait_budget / poll))
    for _ in range(loops):
        updated = store.get_submission(submission_id)
        if not updated:
            raise not_found()
        if updated.status not in {"waiting", "preparing", "judging"}:
            return ok(request, _test_submission_payload(account, contest_id, updated))
        await asyncio.sleep(poll)
    latest = store.get_submission(submission_id)
    if not latest:
        raise not_found()
    return ok(request, _test_submission_payload(account, contest_id, latest))


@router.get("/operator/contests/{contest_id}/judge-history")
async def judge_history(contest_id: str, request: Request, limit: int = 100, cursor: str | None = None):
    require_contest_staff(request, contest_id, "contest.submission.view")
    jobs = [job.model_dump(mode="json") for job in store.judge_jobs.values() if job.contest_id == contest_id]
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    sliced, next_cursor = _page_slice(jobs, limit, cursor)
    return page(
        request,
        sliced,
        next_cursor=next_cursor,
        limit=max(1, min(limit, 300)),
        total_count=len(jobs),
        current_cursor=cursor,
    )


@router.get("/operator/contests/{contest_id}/scoreboard/internal")
def internal_scoreboard(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.scoreboard.view")
    board = store.scoreboard_rows(contest_id, public_view=False)
    public_board = store.scoreboard_rows(contest_id, public_view=True)
    if not board:
        raise not_found()
    rows = [{**row, "visible_to_team": False} for row in board["rows"]]
    return ok(
        request,
        {
            "frozen_public_view": bool(public_board and public_board["frozen"]),
            "operator_live_view": True,
            "problem_stats": board.get("problem_stats", []),
            "rows": rows,
        },
    )


@router.get("/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/internal")
def division_internal_scoreboard(contest_id: str, division_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.scoreboard.view")
    division = store.get_division(contest_id, division_id)
    if not division:
        raise not_found()
    board = store.scoreboard_rows(contest_id, division_id, public_view=False)
    public_board = store.scoreboard_rows(contest_id, division_id, public_view=True)
    if not board:
        raise not_found()
    rows = [{**row, "visible_to_team": False} for row in board["rows"]]
    return ok(
        request,
        {
            "division": division.model_dump(mode="json"),
            "frozen_public_view": bool(public_board and public_board["frozen"]),
            "operator_live_view": True,
            "problem_stats": board.get("problem_stats", []),
            "rows": rows,
        },
    )


class ScoreboardReleaseRequest(BaseModel):
    action: Literal["start", "rank", "next", "all", "undo"]
    rank: int | None = Field(default=None, ge=1)
    expected_step: int | None = Field(default=None, ge=0)
    expected_revision: int | None = Field(default=None, ge=0)


@router.get("/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/release")
async def scoreboard_release(contest_id: str, division_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.scoreboard.view")
    result = store.scoreboard_release(contest_id, division_id)
    if result is None:
        raise not_found()
    return ok(request, result)


@router.post("/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/release")
async def update_scoreboard_release(contest_id: str, division_id: str, payload: ScoreboardReleaseRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.scoreboard.manage")
    try:
        result = store.update_scoreboard_release(contest_id, division_id, payload.action, payload.rank, payload.expected_step, payload.expected_revision)
    except ValueError as error:
        if str(error) == "division not found":
            raise not_found()
        raise AppError(409, "scoreboard_release_conflict", str(error))
    return ok(request, result)


@router.get("/operator/contests/{contest_id}/scoreboard/presentation")
def presentation_scoreboard(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.scoreboard.view")
    from app.services.presentation_board import presentation_board
    return ok(request, presentation_board(contest_id))


@router.get("/operator/contests/{contest_id}/problems")
async def operator_problems(contest_id: str, request: Request):
    account = require_contest_staff(request, contest_id, "contest.problem.view", "contest.problem.review")
    problems = [p for p in store.problems.values() if p.contest_id == contest_id]
    problems.sort(key=lambda item: (item.display_order, item.problem_code, item.title, item.problem_id))
    if not has_contest_permission(account, contest_id, "contest.problem.resource.view"):
        return page(request, [{**problem.model_dump(mode="json"), "editorial": "", "editorial_translations": {}} for problem in problems])
    teams = [
        team
        for team in store.teams.values()
        if team.contest_id == contest_id and not team.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX)
    ]
    total_teams_by_division: dict[str, int] = {}
    valid_team_ids: set[str] = set()
    for team in teams:
        valid_team_ids.add(team.participant_team_id)
        total_teams_by_division[team.division_id] = total_teams_by_division.get(team.division_id, 0) + 1

    solved_teams_by_problem: dict[str, set[str]] = {}
    for submission in store.submissions.values():
        if submission.contest_id != contest_id or submission.status != SubmissionStatus.ACCEPTED:
            continue
        if submission.participant_team_id not in valid_team_ids:
            continue
        solved_teams_by_problem.setdefault(submission.problem_id, set()).add(submission.participant_team_id)

    items = []
    for problem in problems:
        item = problem.model_dump(mode="json")
        item["solved_team_count"] = len(solved_teams_by_problem.get(problem.problem_id, set()))
        item["total_team_count"] = total_teams_by_division.get(problem.division_id, 0)
        items.append(item)
    return page(request, items)


@router.post("/operator/contests/{contest_id}/storage/presign-upload")
async def presign_upload(contest_id: str, payload: PresignUploadRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    try:
        storage_key = object_storage.storage_key(contest_id, payload.category, payload.filename)
    except ValueError:
        raise AppError(422, "invalid_storage_key", "Invalid storage category or filename.")
    return ok(
        request,
        {
            "method": "PUT",
            "storage_key": storage_key,
            "upload_url": object_storage.presigned_put_url(storage_key),
            "content_type": payload.content_type,
        },
    )


@router.post("/operator/contests/{contest_id}/problems")
async def create_problem(contest_id: str, payload: ProblemCreateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    try:
        problem = store.create_problem(
            contest_id=contest_id,
            division_id=payload.division_id,
            problem_code=payload.problem_code,
            title=payload.title,
            statement=payload.statement,
            time_limit_ms=payload.time_limit_ms,
            memory_limit_mb=payload.memory_limit_mb,
            language_resource_limits=_clean_language_resource_limits(payload.language_resource_limits),
            allowed_languages=payload.allowed_languages,
            display_order=payload.display_order,
            default_statement_language=payload.default_statement_language,
            statement_translations={k: v.model_dump() for k, v in payload.statement_translations.items()},
            editorial_translations=payload.editorial_translations,
        )
    except ValueError:
        raise not_found("Contest division is not configured.")
    return ok(request, problem.model_dump(mode="json"))


@router.post("/operator/contests/{contest_id}/problems:copy")
async def copy_problem(contest_id: str, payload: ProblemCopyRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    try:
        problem = store.copy_problem_to_division(
            contest_id=contest_id,
            source_problem_id=payload.source_problem_id,
            target_division_id=payload.target_division_id,
            problem_code=payload.problem_code,
            display_order=payload.display_order,
        )
    except ValueError as error:
        message = str(error)
        if "not found" in message:
            raise not_found(message)
        raise AppError(422, "validation_error", message)
    return ok(request, problem.model_dump(mode="json"))


@router.patch("/operator/contests/{contest_id}/problems/{problem_id}")
async def update_problem(contest_id: str, problem_id: str, payload: ProblemUpdateRequest, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    try:
        values = payload.model_dump(exclude_unset=True)
        if "language_resource_limits" in values:
            values["language_resource_limits"] = _clean_language_resource_limits(
                payload.language_resource_limits,
            )
        problem = store.update_problem(contest_id, problem_id, **values)
    except ValueError as error:
        raise AppError(422, "validation_error", str(error))
    if not problem:
        raise not_found()
    return ok(request, problem.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/problems/{problem_id}")
async def delete_problem(contest_id: str, problem_id: str, request: Request, cancel_rejudging: bool = False):
    account = require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    if cancel_rejudging:
        from app.services import rejudging
        pending = await asyncio.to_thread(rejudging.pending_for_problem, store, contest_id, problem_id)
        if pending and not all(has_contest_permission(account, contest_id, permission)
            for permission in ("contest.submission.view", "contest.problem.test")):
            raise AppError(403, "problem_deletion_rejudging_permission",
                "이 문제에 미처리 재채점 작업이 있습니다. 재채점 권한이 있는 운영자가 작업을 취소한 뒤 삭제해 주세요.")
        for batch_id in pending:
            # Use the normal cancellation locks and lease revocation. Deletion
            # still checks for new or conflicting work before removing data.
            await asyncio.to_thread(rejudging.decide, store, contest_id, batch_id, "cancel", account)
    problem = await asyncio.to_thread(store.delete_problem, contest_id, problem_id)
    if not problem:
        raise not_found()
    return ok(request, problem.model_dump(mode="json"))


@router.get("/operator/contests/{contest_id}/problems/{problem_id}/assets")
async def problem_assets(contest_id: str, problem_id: str, request: Request):
    account = require_contest_staff(request, contest_id, "contest.problem.view", "contest.problem.review")
    try:
        assets = store.problem_assets_for_problem(contest_id, problem_id)
        if not has_contest_permission(account, contest_id, "contest.problem.resource.view"):
            statement_prefixes = (
                f"contests/{contest_id}/problems/{problem_id}/assets/",
                f"contests/{contest_id}/problems/{problem_id}/problem-assets/",
                f"contests/{contest_id}/problem-assets/",
            )
            assets = [asset for asset in assets if asset.mime_type.startswith("image/") and asset.storage_key.startswith(statement_prefixes) and package_role(asset) is None and not is_editor_source_asset(asset)]
    except ValueError:
        raise not_found()
    return page(
        request,
        [{**asset.model_dump(mode="json"), "download_url": object_storage.presigned_get_url(asset.storage_key)} for asset in assets],
    )


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/assets")
async def create_problem_asset(contest_id: str, problem_id: str, payload: ProblemAssetCreateRequest, request: Request, background_tasks: BackgroundTasks):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    _require_storage_namespace(contest_id, payload.storage_key)
    candidate_asset = ProblemAsset(
        contest_id=contest_id,
        problem_id=problem_id,
        original_filename=payload.original_filename,
        storage_key=payload.storage_key,
        mime_type=payload.mime_type,
        file_size=payload.file_size,
        sha256=payload.sha256,
    )
    try:
        verify_active_testcases_with_candidate_asset(contest_id, problem_id, candidate_asset)
    except PackageBuildError as error:
        try:
            object_storage.delete(payload.storage_key)
        except Exception:
            pass
        raise AppError(422, "package_asset_verification_failed", str(error))
    try:
        asset = store.create_problem_asset(
            contest_id=contest_id,
            problem_id=problem_id,
            original_filename=payload.original_filename,
            storage_key=payload.storage_key,
            mime_type=payload.mime_type,
            file_size=payload.file_size,
            sha256=payload.sha256,
        )
    except ValueError:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, asset.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/problems/{problem_id}/assets/{asset_id}")
async def delete_problem_asset(contest_id: str, problem_id: str, asset_id: str, request: Request, background_tasks: BackgroundTasks):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    item = store.delete_problem_asset(contest_id, problem_id, asset_id)
    if not item:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, item.model_dump(mode="json"))


@router.get("/operator/contests/{contest_id}/problems/{problem_id}/testcase-sets")
async def testcase_sets(contest_id: str, problem_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    try:
        items = store.testcase_sets_for_problem(
            contest_id,
            problem_id,
            include_file_sizes=True,
        )
    except ValueError:
        raise not_found()
    return page(request, items)


@router.get("/operator/contests/{contest_id}/problems/{problem_id}/package-status")
async def package_status(contest_id: str, problem_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    try:
        return ok(request, _problem_package_status(contest_id, problem_id))
    except ValueError:
        raise not_found()


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/judge-bundle:warm")
async def warm_judge_bundle(contest_id: str, problem_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    problem = store.problems.get(problem_id)
    if not problem or problem.contest_id != contest_id:
        raise not_found()
    store.enqueue_bundle_warm(contest_id, problem_id)
    try:
        return ok(request, store.problem_judge_bundle_status(contest_id, problem_id))
    except ValueError:
        raise not_found()


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/testcase-sets")
async def create_testcase_set(contest_id: str, problem_id: str, payload: TestcaseSetCreateRequest, request: Request, background_tasks: BackgroundTasks):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    try:
        item = store.create_testcase_set(contest_id, problem_id, payload.is_active)
    except ValueError:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, item.model_dump(mode="json"))


@router.patch("/operator/contests/{contest_id}/problems/{problem_id}/testcase-sets/{testcase_set_id}")
async def update_testcase_set(
    contest_id: str,
    problem_id: str,
    testcase_set_id: str,
    payload: TestcaseSetUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    item = store.update_testcase_set(contest_id, problem_id, testcase_set_id, **payload.model_dump(exclude_unset=True))
    if not item:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, item.model_dump(mode="json"))


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/testcase-sets/{testcase_set_id}/testcases")
async def create_testcase(
    contest_id: str,
    problem_id: str,
    testcase_set_id: str,
    payload: TestcaseCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    _require_storage_namespace(contest_id, payload.input_storage_key, payload.output_storage_key)
    try:
        item = store.add_testcase(
            contest_id=contest_id,
            problem_id=problem_id,
            testcase_set_id=testcase_set_id,
            display_order=payload.display_order,
            input_storage_key=payload.input_storage_key,
            output_storage_key=payload.output_storage_key,
            input_sha256=payload.input_sha256,
            output_sha256=payload.output_sha256,
            time_limit_ms_override=payload.time_limit_ms_override,
            memory_limit_mb_override=payload.memory_limit_mb_override,
        )
    except ValueError:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, item.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/problems/{problem_id}/testcase-sets/{testcase_set_id}")
async def delete_testcase_set(
    contest_id: str,
    problem_id: str,
    testcase_set_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    item = store.delete_testcase_set(contest_id, problem_id, testcase_set_id)
    if not item:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, item.model_dump(mode="json"))


@router.delete("/operator/contests/{contest_id}/problems/{problem_id}/testcase-sets/{testcase_set_id}/testcases/{testcase_id}")
async def delete_testcase(
    contest_id: str,
    problem_id: str,
    testcase_set_id: str,
    testcase_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    item = store.delete_testcase(contest_id, problem_id, testcase_set_id, testcase_id)
    if not item:
        raise not_found()
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, item.model_dump(mode="json"))


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/verified-testcase-sets:zip")
async def create_verified_testcase_set_from_zip(
    contest_id: str,
    problem_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    archive = await file.read()
    if len(archive) > 128 * 1024 * 1024:
        raise AppError(422, "archive_too_large", "zip 파일은 128MB 이하만 업로드할 수 있습니다.")
    batch_id = uuid4().hex[:12]
    cases = []
    for index, (stem, input_bytes, output_bytes) in enumerate(_testcase_pairs_from_zip(archive), start=1):
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-") or f"{index:03d}"
        input_key = f"contests/{contest_id}/problems/{problem_id}/testcases/imports/{batch_id}/{index:03d}-{safe_stem}.in"
        output_key = f"contests/{contest_id}/problems/{problem_id}/testcases/imports/{batch_id}/{index:03d}-{safe_stem}.out"
        object_storage.write_bytes(input_key, input_bytes, "text/plain")
        object_storage.write_bytes(output_key, output_bytes, "text/plain")
        cases.append(
            UploadedTestcase(
                display_order=index,
                input_storage_key=input_key,
                output_storage_key=output_key,
                input_sha256=hashlib.sha256(input_bytes).hexdigest(),
                output_sha256=hashlib.sha256(output_bytes).hexdigest(),
            )
        )
    try:
        result = build_verified_testcase_set(contest_id, problem_id, cases)
    except PackageBuildError as error:
        raise AppError(422, "testcase_verification_failed", str(error))
    except ValueError:
        raise not_found()
    result["imported_archive"] = {"filename": file.filename, "case_count": len(cases), "format": "paired .in/.out by same path stem"}
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, result)


@router.post("/operator/contests/{contest_id}/problems/{problem_id}/verified-testcase-sets")
async def create_verified_testcase_set(
    contest_id: str,
    problem_id: str,
    payload: VerifiedTestcaseSetCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    _require_contest_mutation_open(contest_id)
    for case in payload.cases:
        _require_storage_namespace(contest_id, case.input_storage_key, case.output_storage_key)
    try:
        result = build_verified_testcase_set(
            contest_id,
            problem_id,
            [
                UploadedTestcase(
                    display_order=item.display_order,
                    input_storage_key=item.input_storage_key,
                    output_storage_key=item.output_storage_key,
                    input_sha256=item.input_sha256,
                    output_sha256=item.output_sha256,
                )
                for item in payload.cases
            ],
        )
    except PackageBuildError as error:
        raise AppError(422, "testcase_verification_failed", str(error))
    except ValueError:
        raise not_found()
    except Exception as error:
        raise AppError(422, "testcase_verification_failed", f"unexpected verifier error: {error}")
    _schedule_bundle_warm(background_tasks, contest_id, problem_id)
    return ok(request, result)


class RejudgingCreateRequest(BaseModel):
    submission_id: str | None = Field(default=None, min_length=1, max_length=36)
    problem_id: str | None = Field(default=None, min_length=1, max_length=36)
    team_id: str | None = Field(default=None, min_length=1, max_length=36)
    language: Literal['c99', 'cpp17', 'python311', 'python313', 'java8', 'pypy3', 'kotlin'] | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError('재채점 사유를 입력해 주세요.')
        return value.strip()


def _rejudging_authority(request, contest_id):
    account = require_contest_staff(request, contest_id, 'contest.submission.view')
    require_contest_staff(request, contest_id, 'contest.problem.test')
    return account


@router.post('/operator/contests/{contest_id}/rejudgings')
async def create_rejudging(contest_id: str, payload: RejudgingCreateRequest, request: Request):
    from app.services import rejudging
    account = _rejudging_authority(request, contest_id)
    if bool(payload.submission_id) == bool(payload.problem_id) or (payload.submission_id and (payload.team_id or payload.language)):
        raise AppError(422, 'rejudging_scope_invalid', '개별 제출 또는 문제 전체 중 하나를 선택해 주세요.')
    result = await asyncio.to_thread(rejudging.create, store, contest_id, **payload.model_dump(), actor=account)
    request.state.audit_rejudge = {key: result[key] for key in ('rejudging_id', 'problem_id', 'problem_code', 'problem_title', 'reason', 'scope', 'total')}
    request.state.audit_rejudge['operation'] = 'create'
    return ok(request, result)


@router.get('/operator/contests/{contest_id}/rejudgings')
async def list_rejudgings(contest_id: str, request: Request, offset: int = 0, limit: int = 20):
    from app.services import rejudging
    require_contest_staff(request, contest_id, 'contest.submission.view')
    return ok(request, await asyncio.to_thread(rejudging.list_batches, store, contest_id, max(0, offset), max(1, min(limit, 50))))


@router.get('/operator/contests/{contest_id}/rejudgings/{rejudging_id}')
async def rejudging_detail(contest_id: str, rejudging_id: str, request: Request, offset: int = 0, limit: int = 50):
    from app.services import rejudging
    require_contest_staff(request, contest_id, 'contest.submission.view')
    return ok(request, await asyncio.to_thread(rejudging.detail, store, contest_id, rejudging_id, max(0, offset), max(1, min(limit, 100))))


async def _decide_rejudging(contest_id, rejudging_id, action, request):
    from app.services import rejudging
    account = _rejudging_authority(request, contest_id)
    result = await asyncio.to_thread(rejudging.decide, store, contest_id, rejudging_id, action, account)
    request.state.audit_rejudge = {key: result[key] for key in ('rejudging_id', 'problem_id', 'problem_code', 'problem_title', 'reason', 'total', 'changed_verdicts')}
    request.state.audit_rejudge['operation'] = action
    return ok(request, result)


@router.post('/operator/contests/{contest_id}/rejudgings/{rejudging_id}/apply')
async def apply_rejudging(contest_id: str, rejudging_id: str, request: Request):
    return await _decide_rejudging(contest_id, rejudging_id, 'apply', request)


@router.post('/operator/contests/{contest_id}/rejudgings/{rejudging_id}/cancel')
async def cancel_rejudging(contest_id: str, rejudging_id: str, request: Request):
    return await _decide_rejudging(contest_id, rejudging_id, 'cancel', request)
