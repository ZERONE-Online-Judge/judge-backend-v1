import asyncio
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field, StrictBool, ConfigDict, field_validator, model_validator

from app.models import ContestStatus, ContestVisibility, JudgeNode, now_utc
from app.settings import settings
from app.services.authz import require_service_master
from app.services.contest_roles import title_for_roles
from app.services.errors import AppError, not_found
from app.services.mail_templates import absolute_url, format_korean_datetime, operator_assignment_mail, render_branded_email
from app.services.mail_logs import list_mail_logs, mail_log_filters, mail_log_preview
from app.services.responses import ok, page
from app.services.store import SERVICE_MASTER_OPERATOR_ERROR, store

router = APIRouter(tags=["admin"])


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


def _node_with_activity(node: JudgeNode, jobs: dict | None = None, editor_running: int = 0) -> dict:
    active_since = now_utc() - timedelta(seconds=max(5, settings.judge_node_active_window_seconds))
    heartbeat_age = max(0, int((now_utc() - node.last_heartbeat_at).total_seconds()))
    actual_running = len(
        [
            job
            for job in (jobs or store.judge_jobs).values()
            if job.status == "running" and job.assigned_node_id == node.judge_node_id
        ]
    )
    actual_running += editor_running
    payload = node.model_dump(mode="json")
    payload["is_active"] = node.last_heartbeat_at >= active_since
    payload["heartbeat_age_seconds"] = heartbeat_age
    payload["reported_running_job_count"] = node.running_job_count
    payload["actual_running_job_count"] = actual_running
    payload["running_job_count_mismatch"] = node.running_job_count != actual_running
    return payload


def _pending_queue_ranks(jobs: dict | None = None) -> dict[str, int]:
    pending_jobs = sorted(
        (
            job
            for job in (jobs or store.judge_jobs).values()
            if job.status == "pending"
        ),
        key=lambda job: job.queue_position,
    )
    return {
        job.submission_id: index
        for index, job in enumerate(pending_jobs, start=1)
    }


def _submission_queue_position(submission_id: str, jobs: dict | None = None) -> int | None:
    return _pending_queue_ranks(jobs).get(submission_id)


class ContestCreateRequest(BaseModel):
    title: str | None = None
    organization_name: str
    overview: str | None = None
    visibility: ContestVisibility = ContestVisibility.PUBLIC
    visibility_after_end: ContestVisibility = ContestVisibility.PUBLIC
    status: ContestStatus = ContestStatus.DRAFT
    start_at: datetime | None = None
    end_at: datetime | None = None
    freeze_at: datetime | None = None
    operator_email: EmailStr | None = None
    operator_display_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("operator_display_name")
    @classmethod
    def nonblank_operator_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("표시 이름을 입력해 주세요.")
        return value.strip() if value is not None else None


class ContestDivisionCreateRequest(BaseModel):
    code: str
    name: str
    description: str = ""
    display_order: int = 1


class ContestOperatorCreateRequest(BaseModel):
    email: EmailStr
    display_name: str | None = None


class ServiceNoticeCreateRequest(BaseModel):
    title: str
    summary: str
    body: str
    emergency: bool = False


class ServiceNoticeUpdateRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    body: str | None = None
    emergency: bool | None = None


class ContactInquiryAnswerRequest(BaseModel):
    answer_body: str


class ContestDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_title: str = Field(min_length=1, max_length=255)


@router.get("/admin/users")
def admin_users(request: Request, q: str = Query(default="", max_length=200),
                password: Literal["all", "set", "unset"] = "all",
                sort: Literal["email", "name", "contest_count", "created_at", "password_updated_at"] = "email",
                order: Literal["asc", "desc"] = "asc",
                limit: int = Query(default=25, ge=1, le=100),
                offset: int = Query(default=0, ge=0)):
    require_service_master(request)
    from app.services.admin_users import list_users
    users, total = list_users(q, password, limit, offset, sort, order)
    return page(request, users, limit=limit, current_cursor=str(offset),
                next_cursor=str(offset + limit) if offset + limit < total else None,
                total_count=total)


@router.get("/admin/users/detail")
def admin_user_detail(request: Request, email: str = Query(min_length=1, max_length=254)):
    require_service_master(request)
    from app.services.admin_users import user_detail
    return ok(request, user_detail(email))


@router.get("/admin/dashboard")
async def dashboard(request: Request):
    require_service_master(request)
    jobs = store.judge_jobs
    node_payloads = [_node_with_activity(node, jobs) for node in store.judge_nodes.values()]
    return ok(
        request,
        {
            "contest_count": len(store.contests),
            "pending_jobs": len([job for job in store.judge_jobs.values() if job.status == "pending"]),
            "mail_queue_pending": len([mail for mail in store.mail_queue.values() if mail.status == "pending"]),
            "judge_node_count": len(node_payloads),
            "active_judge_node_count": len([node for node in node_payloads if node["is_active"]]),
        },
    )


@router.get("/admin/contests")
async def admin_contests(request: Request):
    require_service_master(request)
    return page(request, [contest.model_dump(mode="json") for contest in store.contests.values()])


@router.post("/admin/contests")
async def create_contest(payload: ContestCreateRequest, request: Request):
    require_service_master(request)
    if payload.operator_email and store.is_service_master_email(str(payload.operator_email)):
        raise AppError(409, "service_master_operator_immutable", "서비스 마스터는 모든 대회 권한을 자동으로 가지므로 대회 운영자로 추가할 수 없습니다.")
    contest = store.create_contest(
        payload.title,
        payload.organization_name,
        payload.overview,
        payload.start_at,
        payload.end_at,
        payload.freeze_at,
        payload.status,
        visibility=payload.visibility,
        visibility_after_end=payload.visibility_after_end,
    )
    if payload.operator_email:
        try:
            operator = store.upsert_contest_operator(contest.contest_id, str(payload.operator_email), payload.operator_display_name or str(payload.operator_email), protected_master=True)
        except ValueError as exc:
            message = str(exc)
            if message == SERVICE_MASTER_OPERATOR_ERROR:
                raise AppError(409, "service_master_operator_immutable", "서비스 마스터는 모든 대회 권한을 자동으로 가지므로 대회 운영자로 추가할 수 없습니다.")
            if message.startswith("operator email cannot be participant email:"):
                raise AppError(422, "validation_error", message, {"field": "operator_email_conflict"})
            raise AppError(404, "not_found", message)
        content = operator_assignment_mail(
            contest_title=contest.title,
            organization_name=contest.organization_name,
            display_name=operator.display_name,
            role_label=title_for_roles(operator.contest_roles.get(contest.contest_id, ["master"])) or "마스터",
            starts_at=None if contest.status in {ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD} else contest.start_at,
            console_url=absolute_url(f"/operator/contests/{contest.contest_id}"),
        )
        store.enqueue_mail(
            "contest_operator_assigned",
            str(payload.operator_email),
            content.subject,
            content.body_text,
            content.body_html,
            contest_id=contest.contest_id,
        )
    return ok(request, contest.model_dump(mode="json"))


@router.get("/admin/contests/{contest_id}/divisions")
async def admin_contest_divisions(contest_id: str, request: Request):
    require_service_master(request)
    if contest_id not in store.contests:
        raise not_found()
    return ok(
        request,
        [
            division.model_dump(mode="json")
            for division in store.contest_divisions(contest_id)
        ],
    )


@router.delete("/admin/contests/{contest_id}")
def remove_contest(contest_id: str, payload: ContestDeleteRequest, request: Request):
    require_service_master(request)
    from app.services.contest_deletion import delete_contest
    return ok(request, delete_contest(contest_id, payload.confirm_title))


@router.post("/admin/contests/{contest_id}/divisions")
async def create_contest_division(contest_id: str, payload: ContestDivisionCreateRequest, request: Request):
    require_service_master(request)
    try:
        division = store.create_contest_division(contest_id, payload.code, payload.name, payload.description, payload.display_order)
    except ValueError as exc:
        raise AppError(404, "not_found", str(exc))
    return ok(request, division.model_dump(mode="json"))


@router.post("/admin/contests/{contest_id}/operators")
async def create_contest_operator(contest_id: str, payload: ContestOperatorCreateRequest, request: Request):
    require_service_master(request)
    try:
        operator = store.upsert_contest_operator(contest_id, str(payload.email), payload.display_name or str(payload.email), protected_master=True)
    except ValueError as exc:
        message = str(exc)
        if message == SERVICE_MASTER_OPERATOR_ERROR:
            raise AppError(409, "service_master_operator_immutable", "서비스 마스터는 모든 대회 권한을 자동으로 가지므로 대회 운영자로 추가할 수 없습니다.")
        if message.startswith("operator email cannot be participant email:"):
            raise AppError(422, "validation_error", message, {"field": "operator_email_conflict"})
        raise AppError(404, "not_found", message)
    contest = store.contests.get(contest_id)
    if contest:
        content = operator_assignment_mail(
            contest_title=contest.title,
            organization_name=contest.organization_name,
            display_name=operator.display_name,
            role_label=title_for_roles(operator.contest_roles.get(contest_id, ["master"])) or "마스터",
            starts_at=None if contest.status in {ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD} else contest.start_at,
            console_url=absolute_url(f"/operator/contests/{contest_id}"),
        )
        store.enqueue_mail(
            "contest_operator_assigned",
            str(payload.email),
            content.subject,
            content.body_text,
            content.body_html,
            contest_id=contest_id,
        )
    return ok(request, operator.model_dump(mode="json"))


@router.get("/admin/service-managers")
async def service_managers(request: Request):
    require_service_master(request)
    return page(request, [staff.model_dump(mode="json") for staff in store.staff_accounts.values()])


@router.get("/admin/service-notices")
async def admin_service_notices(request: Request):
    require_service_master(request)
    notices = [notice.model_dump(mode="json") for notice in store.service_notices.values()]
    notices.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    return page(request, notices)


@router.post("/admin/service-notices")
async def create_service_notice(payload: ServiceNoticeCreateRequest, request: Request):
    require_service_master(request)
    notice = store.create_service_notice(payload.title, payload.summary, payload.body, payload.emergency)
    return ok(request, notice.model_dump(mode="json"))


@router.patch("/admin/service-notices/{notice_id}")
async def update_service_notice(notice_id: str, payload: ServiceNoticeUpdateRequest, request: Request):
    require_service_master(request)
    notice = store.update_service_notice(notice_id, **payload.model_dump(exclude_unset=True))
    if not notice:
        raise AppError(404, "not_found", "Service notice was not found.")
    return ok(request, notice.model_dump(mode="json"))


@router.delete("/admin/service-notices/{notice_id}")
async def delete_service_notice(notice_id: str, request: Request):
    require_service_master(request)
    deleted = store.delete_service_notice(notice_id)
    if not deleted:
        raise AppError(404, "not_found", "Service notice was not found.")
    return ok(request, {"service_notice_id": notice_id, "deleted": True})


@router.get("/admin/contact-inquiries")
async def admin_contact_inquiries(request: Request):
    require_service_master(request)
    inquiries = [item.model_dump(mode="json") for item in store.contact_inquiries.values()]
    inquiries.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return page(request, inquiries)


@router.get("/admin/audit-logs")
def admin_audit_logs(
    request: Request,
    scope: str | None = None,
    contest_id: str | None = None,
    actor_email: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
):
    require_service_master(request)
    safe_scope = scope if scope in {"admin", "operator"} else None
    logs, next_cursor, total_count = store.list_operational_audit_logs(
        scope=safe_scope,
        contest_id=contest_id or None,
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


@router.get("/admin/access-logs")
def admin_access_logs(
    request: Request,
    account_scope: str | None = None,
    contest_id: str | None = None,
    email: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
):
    require_service_master(request)
    safe_scope = account_scope if account_scope in {"general", "participant"} else None
    logs, next_cursor, total_count = store.list_access_logs(
        account_scope=safe_scope,
        contest_id=contest_id or None,
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


@router.get("/admin/access-log-stats")
def admin_access_log_stats(request: Request, contest_id: str | None = None):
    require_service_master(request)
    return ok(request, store.access_log_stats(contest_id=contest_id or None))


@router.post("/admin/contact-inquiries/{inquiry_id}/answer")
async def answer_contact_inquiry(inquiry_id: str, payload: ContactInquiryAnswerRequest, request: Request):
    account = require_service_master(request)
    answer_body = payload.answer_body.strip()
    if not answer_body:
        raise AppError(422, "validation_error", "Answer body is required.")
    inquiry = store.answer_contact_inquiry(inquiry_id, answer_body, str(account.email))
    if not inquiry:
        raise not_found()
    store.enqueue_mail(
        "contact_inquiry_answered",
        str(inquiry.sender_email),
        f"[ZOJ] 문의 답변: {inquiry.title}",
        "\n".join(
            [
                f"{inquiry.sender_name}님, 안녕하세요.",
                "문의하신 내용에 대한 답변을 전달드립니다.",
                "",
                f"문의 제목: {inquiry.title}",
                "",
                "새 답변:",
                answer_body,
                "",
                "문의 내용:",
                inquiry.body,
            ]
        ),
        render_branded_email(
            title=f"문의 답변: {inquiry.title}",
            eyebrow="서비스 문의 답변",
            preheader="문의하신 내용에 대한 답변을 전달드립니다.",
            body=[
                f"{inquiry.sender_name}님, 안녕하세요.",
                "문의하신 내용에 대한 답변을 전달드립니다.",
            ],
            meta=[
                ("문의 제목", inquiry.title),
                ("답변자", str(account.email)),
                ("답변 시각", f"{format_korean_datetime(inquiry.answered_at)} KST" if inquiry.answered_at else "-"),
            ],
            sections=[
                ("새 답변", answer_body),
                ("문의 본문", inquiry.body),
            ],
        ),
    )
    return ok(request, inquiry.model_dump(mode="json"))


@router.get("/admin/judge/dashboard")
async def judge_dashboard(
    request: Request,
    include_queue: bool = False,
    status_filter: str = "pending,running",
    limit: int = 50,
    cursor: str | None = None,
):
    require_service_master(request)
    jobs = list(store.judge_jobs.values())
    jobs_by_id = {job.judge_job_id: job for job in jobs}
    from app.services import editor_execution
    editor_stats = await asyncio.to_thread(editor_execution.stats, store._session)
    node_payloads = [_node_with_activity(node, jobs_by_id, editor_stats["running_by_node"].get(node.judge_node_id, 0)) for node in store.judge_nodes.values()]
    queue_payload: list[dict] = []
    queue_page = {"limit": max(1, min(limit, 300)), "next_cursor": None}
    if include_queue:
        allowed = {token.strip() for token in status_filter.split(",") if token.strip()}
        if not allowed:
            allowed = {"pending", "running"}
        filtered = [job for job in jobs if job.status in allowed]
        filtered.sort(key=lambda item: item.created_at, reverse=True)
        queue_slice, next_cursor = _page_slice(filtered, limit, cursor)
        queue_payload = [job.model_dump(mode="json") for job in queue_slice]
        queue_page["next_cursor"] = next_cursor
    return ok(
        request,
        {
            "nodes": node_payloads,
            "queue": queue_payload,
            "queue_page": queue_page,
            "queue_stats": {
                "pending_count": len([job for job in jobs if job.status == "pending"]) + editor_stats["pending_count"],
                "editor_pending_count": editor_stats["pending_count"],
                "running_count": len([job for job in jobs if job.status == "running"]) + editor_stats["running_count"],
                "editor_running_count": editor_stats["running_count"],
                "succeeded_count": len([job for job in jobs if job.status == "succeeded"]),
            },
        },
    )


@router.get("/admin/judge/nodes/{node_id}/logs")
async def judge_node_logs(
    node_id: str,
    request: Request,
    limit: int = 200,
    cursor: str | None = None,
):
    require_service_master(request)
    result = store.list_judge_agent_logs(node_id, limit=limit, cursor=cursor)
    if result is None:
        raise not_found()
    logs, next_cursor, total_count = result
    return page(
        request,
        [item.model_dump(mode="json") for item in logs],
        next_cursor=next_cursor,
        limit=max(1, min(limit, 500)),
        total_count=total_count,
        current_cursor=cursor,
    )


@router.get("/admin/judge/submissions")
async def judge_submissions(
    request: Request,
    limit: int = 100,
    cursor: str | None = None,
    include_source: bool = False,
    contest_id: str | None = None,
    division_id: str | None = None,
):
    require_service_master(request)
    latest, next_cursor, total_count = store.list_submissions(
        contest_id=contest_id,
        division_id=division_id,
        include_source=include_source,
        limit=limit,
        cursor=cursor,
    )
    submission_ids = [submission.submission_id for submission in latest]
    job_by_submission_id = store.judge_jobs_by_submission_ids(submission_ids)
    contests = store.contests_by_ids([submission.contest_id for submission in latest])
    problems = store.problems_by_ids([submission.problem_id for submission in latest])
    divisions = store.divisions_by_ids([submission.division_id for submission in latest])
    teams = store.teams_by_ids([submission.participant_team_id for submission in latest])
    nodes = store.judge_nodes_by_ids(
        [
            job.assigned_node_id
            for job in job_by_submission_id.values()
            if job.assigned_node_id
        ]
    )
    queue_rank_by_submission_id = store.pending_queue_ranks(contest_id=contest_id)
    source_lengths = store.submission_source_lengths(submission_ids)
    testcase_counts = store.active_testcase_counts_by_problem_ids(
        list({submission.problem_id for submission in latest})
    )
    data = []
    for submission in latest:
        problem = problems.get(submission.problem_id)
        contest = contests.get(submission.contest_id)
        division = divisions.get(submission.division_id)
        team = teams.get(submission.participant_team_id)
        member = next((item for item in (team.members if team else []) if item.team_member_id == submission.team_member_id), None)
        job = job_by_submission_id.get(submission.submission_id)
        node = nodes.get(job.assigned_node_id) if job and job.assigned_node_id else None
        case_count = testcase_counts.get(submission.problem_id, 0)
        submission_payload = submission.model_dump(mode="json")
        submission_payload["queue_position"] = queue_rank_by_submission_id.get(submission.submission_id)
        submission_payload["source_code_length"] = source_lengths.get(submission.submission_id, 0)
        if not include_source:
            submission_payload["source_code"] = None
        data.append(
            {
                "submission": submission_payload,
                "contest": {"contest_id": contest.contest_id, "title": contest.title} if contest else None,
                "division": {"division_id": division.division_id, "name": division.name} if division else None,
                "problem": {
                    "problem_id": problem.problem_id,
                    "problem_code": problem.problem_code,
                    "title": problem.title,
                    "time_limit_ms": problem.time_limit_ms,
                    "memory_limit_mb": problem.memory_limit_mb,
                } if problem else None,
                "team": {"participant_team_id": team.participant_team_id, "team_name": team.team_name} if team else None,
                "member": {"team_member_id": member.team_member_id, "name": member.name, "email": member.email} if member else None,
                "judge_job": job.model_dump(mode="json") if job else None,
                "judge_node": node.model_dump(mode="json") if node else None,
                "active_testcase_count": case_count,
                "queue_position": queue_rank_by_submission_id.get(submission.submission_id),
            }
        )
    return page(
        request,
        data,
        next_cursor=next_cursor,
        limit=max(1, min(limit, 300)),
        total_count=total_count,
        current_cursor=cursor,
    )


@router.get("/admin/judge/submissions/{submission_id}")
async def judge_submission_detail(submission_id: str, request: Request):
    require_service_master(request)
    submission = store.get_submission(submission_id)
    if not submission:
        raise not_found()
    contests = store.contests_by_ids([submission.contest_id])
    problems = store.problems_by_ids([submission.problem_id])
    divisions = store.divisions_by_ids([submission.division_id])
    teams = store.teams_by_ids([submission.participant_team_id])
    jobs = store.judge_jobs
    nodes = store.judge_nodes
    testcase_sets = store.testcase_sets
    testcases = store.testcases

    testcase_by_set: dict[str, list] = {}
    for testcase in testcases.values():
        testcase_by_set.setdefault(testcase.testcase_set_id, []).append(testcase)

    problem = problems.get(submission.problem_id)
    contest = contests.get(submission.contest_id)
    division = divisions.get(submission.division_id)
    team = teams.get(submission.participant_team_id)
    member = next((item for item in (team.members if team else []) if item.team_member_id == submission.team_member_id), None)
    job = next((item for item in jobs.values() if item.submission_id == submission.submission_id), None)
    node = nodes.get(job.assigned_node_id) if job and job.assigned_node_id else None
    active_set = next((item for item in testcase_sets.values() if item.problem_id == submission.problem_id and item.is_active), None)
    case_count = len(testcase_by_set.get(active_set.testcase_set_id, [])) if active_set else 0
    queue_position = _submission_queue_position(submission.submission_id, jobs)
    return ok(
        request,
        {
            "submission": {
                **submission.model_dump(mode="json"),
                "queue_position": queue_position,
                "source_code_length": len((submission.source_code or "").encode("utf-8")),
            },
            "contest": {"contest_id": contest.contest_id, "title": contest.title} if contest else None,
            "division": {"division_id": division.division_id, "name": division.name} if division else None,
            "problem": {
                "problem_id": problem.problem_id,
                "problem_code": problem.problem_code,
                "title": problem.title,
                "time_limit_ms": problem.time_limit_ms,
                "memory_limit_mb": problem.memory_limit_mb,
            } if problem else None,
            "team": {"participant_team_id": team.participant_team_id, "team_name": team.team_name} if team else None,
            "member": {"team_member_id": member.team_member_id, "name": member.name, "email": member.email} if member else None,
            "judge_job": job.model_dump(mode="json") if job else None,
            "judge_node": node.model_dump(mode="json") if node else None,
            "active_testcase_count": case_count,
            "queue_position": queue_position,
        },
    )


@router.get("/admin/judge/submissions/{submission_id}/status:wait")
async def judge_submission_status_wait(
    submission_id: str,
    request: Request,
    wait_seconds: float = 2.0,
    poll_interval_seconds: float = 0.25,
):
    require_service_master(request)
    submission = store.get_submission(submission_id, include_source=False)
    if not submission:
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
            payload["queue_position"] = _submission_queue_position(submission_id)
            payload["source_code"] = None
            return ok(request, payload)
        await asyncio.sleep(poll)
    latest = store.get_submission(submission_id, include_source=False)
    if not latest:
        raise not_found()
    payload = latest.model_dump(mode="json")
    payload["queue_position"] = _submission_queue_position(submission_id)
    payload["source_code"] = None
    return ok(request, payload)


@router.get("/admin/mail-queue")
async def mail_queue(request: Request):
    require_service_master(request)
    return page(request, [mail.model_dump(mode="json") for mail in store.mail_queue.values()])


@router.get("/admin/mail-logs/{mail_id}/preview")
def admin_mail_preview(mail_id: str, request: Request):
    require_service_master(request)
    return ok(request, mail_log_preview(mail_id))


@router.get("/admin/mail-logs")
async def admin_mail_logs(request: Request, filters: dict = Depends(mail_log_filters), contest_id: str | None = None):
    require_service_master(request)
    logs, next_cursor, total = list_mail_logs(contest_id=contest_id or None, **filters)
    return page(request, logs, next_cursor=next_cursor, limit=filters["limit"], total_count=total, current_cursor=filters["cursor"])


class NodeWorkloadsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_zoj: StrictBool
    allow_editor: StrictBool

    @model_validator(mode="after")
    def one_workload(self):
        if not self.allow_zoj and not self.allow_editor:
            raise ValueError("ZOJ 또는 Editor 중 하나 이상 선택하세요.")
        return self


@router.patch("/admin/judge/nodes/{node_id}/workloads")
async def update_node_workloads(node_id: str, payload: NodeWorkloadsRequest, request: Request):
    require_service_master(request)
    from sqlalchemy import select
    from app.orm_models import JudgeNodeRow
    def save():
        with store._session() as db:
            node = db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.judge_node_id == node_id).with_for_update())
            if not node:
                raise not_found()
            node.allow_zoj, node.allow_editor = payload.allow_zoj, payload.allow_editor
            db.commit()
            return {"judge_node_id": node_id, **payload.model_dump()}
    return ok(request, await asyncio.to_thread(save))
