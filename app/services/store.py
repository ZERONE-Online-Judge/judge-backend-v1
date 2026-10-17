from datetime import datetime, timedelta, timezone
from copy import deepcopy
import base64
import gzip
import hashlib
import json
import math
import secrets
from zoneinfo import ZoneInfo
from fastapi.encoders import jsonable_encoder

from sqlalchemy import LargeBinary, cast, case as sql_case, delete, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only, object_session, selectinload

from app.database import SessionLocal, create_schema
from app.services.request_sessions import current_session
from app.services.contest_visibility import contest_is_public, contest_hidden_by_status, normalize_private_resources
from app.services.errors import AppError, permission_denied
from app.settings import settings
from app.problem_languages import validate_statement_fields
from app.judge_languages import LANGUAGES, NEW_LANGUAGES, ICPC_CAPABILITY, require_allowed, validate_languages
from app.services.package_builder import is_editor_source_asset
from app.models import (
    AccessLog,
    Contest,
    ContestDivision,
    ContestNotice,
    ContestQuestion,
    ContestQuestionAnswer,
    ContestResourceAccess,
    ContactInquiry,
    ScoreboardFreezeMode,
    ScoreboardReleaseMode,
    ContestStatus,
    ContestVisibility,
    JudgeAgentLog,
    JudgeJob,
    JudgeJobStatus,
    JudgeNode,
    JUDGE_FINAL_STATUSES,
    JUDGE_PROGRESS_STATUSES,
    MailQueueItem,
    OperationalAuditLog,
    ParticipantTeam,
    Problem,
    ProblemAsset,
    ServiceNotice,
    StaffAccount,
    Submission,
    SubmissionStatus,
    Testcase,
    TestcaseSet,
    TeamMember,
    TeamMemberRole,
    demo_times,
    new_id,
    now_utc,
)
from app.orm_models import (
    AccessLogRow,
    ContestDivisionRow,
    ContestNoticeRow,
    ContestQuestionAnswerRow,
    ContestQuestionRow,
    ContestRow,
    ContactInquiryRow,
    GeneralSessionRow,
    BundleWarmQueueItemRow,
    JudgeAgentLogRow,
    JudgeJobRow,
    JudgeNodeRow,
    MailQueueItemRow,
    OtpCodeRow,
    OperationalAuditLogRow,
    ParticipantTeamRow,
    ParticipantPreviewSessionRow,
    ProblemAssetRow,
    ProblemRow,
    ServiceNoticeRow,
    StaffAccountRow,
    StaffSessionRow,
    SubmissionRow,
    ScoreboardReleaseRow,
    TeamMemberRow,
    TeamSessionRow,
    TestcaseRow,
    TestcaseSetRow,
)
from app.services.contest_roles import permissions_for_roles, roles_for_scopes, title_for_roles, title_for_scopes
from app.services.automatic_notices import compact_automatic_notice, legacy_scheduled_notice, scheduled_notice_copy, scheduled_notice_id
from app.services.notice_countdown import LABELS as COUNTDOWN_LABELS, countdown_template, notice_template, render_notice
from app.services.security import decode_session_token, hash_password, new_session_token, token_hash
from app.services.node_credentials import valid_node_credential
from app.services.result_cache import cached_result
from app.services import rejudging
from app.services.storage import object_storage
from app.services.mail_templates import (
    absolute_url,
    contest_reminder_mail,
    login_verification_mail,
    participant_invite_mail,
    render_basic_html,
)

STAFF_OTP_SCOPE = "__staff__"
GENERAL_OTP_SCOPE = "__general__"
OPERATOR_TEST_TEAM_PREFIX = "__operator_test__"
SERVICE_MASTER_OPERATOR_ERROR = "service master cannot be contest operator"
KST = ZoneInfo("Asia/Seoul")


class SessionConflictError(Exception):
    def __init__(self, details: dict) -> None:
        self.details = details


def is_internal_mail_recipient(email: str | None) -> bool:
    normalized = (email or "").strip().lower()
    return (
        not normalized
        or normalized.endswith("@local.zoj")
        or normalized.startswith("operator-test+")
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _schedule_status(status: str, start_at: datetime, end_at: datetime, now: datetime) -> str:
    if status in {
        ContestStatus.DRAFT.value,
        ContestStatus.SCHEDULE_TBD.value,
        ContestStatus.ENDED.value,
        ContestStatus.FINALIZED.value,
        ContestStatus.ARCHIVED.value,
    }:
        return status
    start = _aware(start_at)
    end = _aware(end_at)
    if start is None or end is None:
        return status
    if now >= end:
        return ContestStatus.ENDED.value
    if start <= now < end:
        return ContestStatus.RUNNING.value
    if now < start and status == ContestStatus.RUNNING.value:
        return ContestStatus.OPEN.value
    return status


def _valid_session_token(token: str, expected_type: str) -> bool:
    if token.count(".") != 2:
        return True
    return decode_session_token(token, expected_type) is not None


def _contest(row: ContestRow, *, release_locked: bool | None = None) -> Contest:
    db = object_session(row)
    if release_locked is None:
        release_locked = bool(db and db.scalar(select(ScoreboardReleaseRow.division_id).where(
            ScoreboardReleaseRow.contest_id == row.contest_id,
            ScoreboardReleaseRow.mode != "not_started",
        ).limit(1)))
    return Contest(
        contest_id=row.contest_id,
        title=row.title,
        organization_name=row.organization_name,
        overview=row.overview,
        status=ContestStatus(row.status),
        visibility=ContestVisibility(row.visibility or "public"),
        visibility_after_end=ContestVisibility(row.visibility_after_end or "public"),
        start_at=_aware(row.start_at),
        end_at=_aware(row.end_at),
        freeze_at=_aware(row.freeze_at),
        problem_access_after_end=ContestResourceAccess(row.problem_access_after_end or "private"),
        scoreboard_access_after_end=ContestResourceAccess(row.scoreboard_access_after_end or "private"),
        submission_access_after_end=ContestResourceAccess(row.submission_access_after_end or "private"),
        board_access_after_end=ContestResourceAccess(row.board_access_after_end or "participants"),
        board_write_after_end=bool(row.board_write_after_end),
        notice_access_after_end=ContestResourceAccess(row.notice_access_after_end or "public"),
        editorial_access_after_end=ContestResourceAccess(row.editorial_access_after_end or "private"),
        scoreboard_freeze_mode=ScoreboardFreezeMode(row.scoreboard_freeze_mode or "auto"),
        scoreboard_release_mode=ScoreboardReleaseMode(row.scoreboard_release_mode or "manual"),
        scoreboard_release_locked=release_locked,
        mock_judging_enabled=bool(row.mock_judging_enabled),
        participant_progress_visible=bool(row.participant_progress_visible),
        mock_judging_progress_visible=bool(row.mock_judging_progress_visible),
        emergency_notice=render_notice(compact_automatic_notice(row.emergency_notice), row, now_utc()),
        emergency_notice_template=notice_template(row.emergency_notice),
        created_at=_aware(row.created_at),
    )


def _division(row: ContestDivisionRow) -> ContestDivision:
    return ContestDivision(
        division_id=row.division_id,
        contest_id=row.contest_id,
        code=row.code,
        name=row.name,
        description=row.description,
        display_order=row.display_order,
        created_at=_aware(row.created_at),
    )


def _team(row: ParticipantTeamRow) -> ParticipantTeam:
    def _member_role(value: str | None) -> TeamMemberRole:
        try:
            return TeamMemberRole(value or TeamMemberRole.MEMBER.value)
        except ValueError:
            return TeamMemberRole.MEMBER

    return ParticipantTeam(
        participant_team_id=row.participant_team_id,
        contest_id=row.contest_id,
        division_id=row.division_id,
        team_name=row.team_name,
        status=row.status,
        members=[
            TeamMember(
                team_member_id=member.team_member_id,
                role=_member_role(member.role),
                name=member.name,
                email=member.email,
                active_sessions=member.active_sessions,
                last_login_at=_aware(member.last_login_at),
                last_session_seen_at=None,
            )
            for member in row.members
        ],
        created_at=_aware(row.created_at),
    )


def _problem(row: ProblemRow) -> Problem:
    return Problem(
        problem_id=row.problem_id,
        contest_id=row.contest_id,
        division_id=row.division_id,
        problem_code=row.problem_code,
        title=row.title,
        statement=row.statement,
        editorial=row.editorial or "",
        default_statement_language=row.default_statement_language or "ko",
        statement_translations=row.statement_translations or {},
        editorial_translations=row.editorial_translations or {},
        time_limit_ms=row.time_limit_ms,
        memory_limit_mb=row.memory_limit_mb,
        language_resource_limits=row.language_resource_limits or {},
        allowed_languages=row.allowed_languages or list(LANGUAGES),
        display_order=row.display_order,
    )


def _asset(row: ProblemAssetRow) -> ProblemAsset:
    return ProblemAsset(
        asset_id=row.asset_id,
        contest_id=row.contest_id,
        problem_id=row.problem_id,
        original_filename=row.original_filename,
        storage_key=row.storage_key,
        mime_type=row.mime_type,
        file_size=row.file_size,
        sha256=row.sha256,
        source_language=row.source_language,
        asset_status=row.asset_status,
        created_at=_aware(row.created_at),
    )


def _testcase_set(row: TestcaseSetRow) -> TestcaseSet:
    return TestcaseSet(
        testcase_set_id=row.testcase_set_id,
        problem_id=row.problem_id,
        version=row.version,
        is_active=row.is_active,
        created_at=_aware(row.created_at),
    )


def _testcase(
    row: TestcaseRow,
    *,
    input_size_bytes: int | None = None,
    output_size_bytes: int | None = None,
) -> Testcase:
    return Testcase(
        testcase_id=row.testcase_id,
        testcase_set_id=row.testcase_set_id,
        display_order=row.display_order,
        input_storage_key=row.input_storage_key,
        output_storage_key=row.output_storage_key,
        input_sha256=row.input_sha256,
        output_sha256=row.output_sha256,
        input_size_bytes=input_size_bytes,
        output_size_bytes=output_size_bytes,
        time_limit_ms_override=row.time_limit_ms_override,
        memory_limit_mb_override=row.memory_limit_mb_override,
        created_at=_aware(row.created_at),
    )


def _staff_title(row: StaffAccountRow, contest_id: str) -> str | None:
    if row.is_service_master:
        return "마스터"
    roles = json.loads(row.contest_roles or "{}").get(contest_id, [])
    if roles:
        return title_for_roles(roles)
    return title_for_scopes(json.loads(row.contest_scopes or "{}").get(contest_id, []))


def _submission_staff_titles(rows, db: Session) -> dict[tuple[str, str], str | None]:
    owners = {(row.contest_id, row.submitted_by_email.lower()) for row in rows if row.submission_kind in {"operator_test", "participant_preview"} and row.submitted_by_email}
    if not owners:
        return {}
    accounts = db.scalars(select(StaffAccountRow).where(func.lower(StaffAccountRow.email).in_({email for _, email in owners}))).all()
    by_email = {str(account.email).lower(): account for account in accounts}
    return {(contest_id, email): _staff_title(by_email[email], contest_id) for contest_id, email in owners if email in by_email}


def _submission(row: SubmissionRow, include_source: bool = True, *, staff_titles: dict[tuple[str, str], str | None] | None = None) -> Submission:
    return Submission(
        submission_id=row.submission_id,
        contest_id=row.contest_id,
        division_id=row.division_id,
        problem_id=row.problem_id,
        participant_team_id=row.participant_team_id,
        team_member_id=row.team_member_id,
        submission_kind=row.submission_kind or "participant",
        submitted_by_name=row.submitted_by_name,
        submitted_by_title=(staff_titles or {}).get((row.contest_id, (row.submitted_by_email or "").lower())),
        submitted_by_email=row.submitted_by_email,
        language=row.language,
        source_code=row.source_code if include_source else "",
        status=SubmissionStatus(row.status),
        submitted_at=_aware(row.submitted_at),
        status_updated_at=_aware(row.status_updated_at),
        compile_message=row.compile_message,
        judge_message=row.judge_message,
        failed_testcase_order=row.failed_testcase_order,
        progress_current=row.progress_current,
        progress_total=row.progress_total,
        runtime_ms=row.runtime_ms,
        memory_kb=row.memory_kb,
    )


def _normalize_source_code(source_code: str) -> str:
    return source_code.replace("\r\n", "\n").replace("\r", "\n")


def _job(row: JudgeJobRow) -> JudgeJob:
    return JudgeJob(
        judge_job_id=row.judge_job_id,
        submission_id=row.submission_id,
        contest_id=row.contest_id,
        division_id=row.division_id,
        status=JudgeJobStatus(row.status),
        queue_position=row.queue_position,
        assigned_node_id=row.assigned_node_id,
        lease_token=row.lease_token,
        leased_at=_aware(row.leased_at),
        created_at=_aware(row.created_at),
    )


def _node(row: JudgeNodeRow) -> JudgeNode:
    return JudgeNode(
        judge_node_id=row.judge_node_id,
        node_name=row.node_name,
        total_slots=row.total_slots,
        free_slots=row.free_slots,
        running_job_count=row.running_job_count,
        agent_version=row.agent_version,
        last_heartbeat_at=_aware(row.last_heartbeat_at),
        schedulable=row.schedulable,
        allow_zoj=row.allow_zoj,
        allow_editor=row.allow_editor,
        execution_capabilities=row.execution_capabilities or [],
        execution_revision=row.execution_revision,
    )


def _agent_log(row: JudgeAgentLogRow) -> JudgeAgentLog:
    return JudgeAgentLog(
        judge_agent_log_id=row.judge_agent_log_id,
        judge_node_id=row.judge_node_id,
        node_name=row.node_name,
        level=row.level,
        message=row.message,
        created_at=_aware(row.created_at),
    )


def _audit_log(row: OperationalAuditLogRow) -> OperationalAuditLog:
    try:
        details = json.loads(row.details or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}

    return OperationalAuditLog(
        operational_audit_log_id=row.operational_audit_log_id,
        scope=row.scope,
        action=row.action,
        method=row.method,
        path=row.path,
        status_code=row.status_code,
        actor_email=row.actor_email,
        actor_name=row.actor_name,
        actor_role=row.actor_role,
        contest_id=row.contest_id,
        client_ip=row.client_ip,
        user_agent=row.user_agent,
        request_id=row.request_id,
        details=details,
        created_at=_aware(row.created_at),
    )


def _audit_log_with_contest(row: OperationalAuditLogRow, contest_titles: dict[str, str]) -> OperationalAuditLog:
    item = _audit_log(row)
    if item.contest_id and "contest_title" not in item.details:
        title = contest_titles.get(item.contest_id)
        if title:
            item.details = {**item.details, "contest_title": title}
    return item


def _access_log(row: AccessLogRow) -> AccessLog:
    try:
        details = json.loads(row.details or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}

    return AccessLog(
        access_log_id=row.access_log_id,
        event_type=row.event_type,
        account_scope=row.account_scope,
        email=row.email,
        display_name=row.display_name,
        contest_id=row.contest_id,
        contest_title=row.contest_title,
        participant_team_id=row.participant_team_id,
        team_name=row.team_name,
        team_member_id=row.team_member_id,
        member_name=row.member_name,
        actor_role=row.actor_role,
        session_id=row.session_id,
        client_ip=row.client_ip,
        user_agent=row.user_agent,
        request_id=row.request_id,
        details=details,
        created_at=_aware(row.created_at),
    )


def _notice(row: ServiceNoticeRow) -> ServiceNotice:
    return ServiceNotice(
        service_notice_id=row.service_notice_id,
        title=row.title,
        summary=row.summary,
        body=row.body,
        emergency=row.emergency,
        published_at=_aware(row.published_at),
    )


def _contest_notice(row: ContestNoticeRow) -> ContestNotice:
    body = compact_automatic_notice(row.body) if row.emergency else row.body
    template = notice_template(row.body)
    if template:
        db = object_session(row)
        body = render_notice(body, db.get(ContestRow, row.contest_id) if db else None, now_utc())
    title = row.title
    if body != row.body:
        if title == "대회 운영 시간이 변경되었습니다":
            title = "대회 일정 변경"
        elif title == "스코어보드 공개됨":
            title = "스코어보드 안내"
    return ContestNotice(
        contest_notice_id=row.contest_notice_id,
        contest_id=row.contest_id,
        title=title,
        body=body,
        body_template=template,
        pinned=row.pinned,
        emergency=row.emergency,
        visibility=row.visibility,
        created_by_email=row.created_by_email,
        published_at=_aware(row.published_at),
        updated_at=_aware(row.updated_at),
    )


def _answer(
    row: ContestQuestionAnswerRow,
    created_by_name: str | None = None,
    created_by_role: str | None = None,
    created_by_team_name: str | None = None,
    created_by_division_name: str | None = None,
    created_by_title: str | None = None,
) -> ContestQuestionAnswer:
    return ContestQuestionAnswer(
        contest_answer_id=row.contest_answer_id,
        contest_question_id=row.contest_question_id,
        contest_id=row.contest_id,
        body=row.body,
        visibility=row.visibility,
        created_by_email=row.created_by_email,
        created_by_name=created_by_name,
        created_by_title=created_by_title,
        created_by_role=created_by_role,
        created_by_team_name=created_by_team_name,
        created_by_division_name=created_by_division_name,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _answer_for_view(row: ContestQuestionAnswerRow, db) -> ContestQuestionAnswer:
    email = (row.created_by_email or "").strip().lower()
    if not email:
        return _answer(row)

    member = db.scalar(
        select(TeamMemberRow).where(
            TeamMemberRow.contest_id == row.contest_id,
            TeamMemberRow.email == email,
        )
    )
    if member:
        team = db.get(ParticipantTeamRow, member.participant_team_id)
        division = db.get(ContestDivisionRow, team.division_id) if team else None
        return _answer(
            row,
            member.name,
            "participant",
            team.team_name if team else None,
            division.name if division else None,
        )

    staff = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
    if staff:
        return _answer(row, staff.display_name, "operator", created_by_title=_staff_title(staff, row.contest_id))

    return _answer(row)


def _question(
    row: ContestQuestionRow,
    team: ParticipantTeamRow | None = None,
    member: TeamMemberRow | None = None,
    division: ContestDivisionRow | None = None,
    answers: list[ContestQuestionAnswerRow] | None = None,
    answer_mapper=None,
) -> ContestQuestion:
    map_answer = answer_mapper or _answer
    return ContestQuestion(
        contest_question_id=row.contest_question_id,
        contest_id=row.contest_id,
        participant_team_id=row.participant_team_id,
        team_member_id=row.team_member_id,
        title=row.title,
        body=row.body,
        visibility=row.visibility,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        team_name=team.team_name if team else None,
        division_name=division.name if division else None,
        author_name=member.name if member else None,
        author_email=member.email if member else None,
        answers=[
            map_answer(answer)
            for answer in sorted(
                answers if answers is not None else row.answers,
                key=lambda answer: (answer.created_at, answer.contest_answer_id),
            )
        ],
    )


def _question_for_view(
    row: ContestQuestionRow,
    db,
    answers: list[ContestQuestionAnswerRow] | None = None,
) -> ContestQuestion:
    team = db.get(ParticipantTeamRow, row.participant_team_id)
    member = db.get(TeamMemberRow, row.team_member_id)
    division = db.get(ContestDivisionRow, team.division_id) if team else None
    return _question(
        row,
        team=team,
        member=member,
        division=division,
        answers=answers,
        answer_mapper=lambda answer: _answer_for_view(answer, db),
    )


def _staff(row: StaffAccountRow) -> StaffAccount:
    return StaffAccount(
        staff_account_id=row.staff_account_id,
        email=row.email,
        display_name=row.display_name,
        is_service_master=row.is_service_master,
        permissions=[item for item in row.permissions.split(",") if item],
        contest_scopes=json.loads(row.contest_scopes or "{}"),
        contest_roles={
            contest_id: json.loads(row.contest_roles or "{}").get(contest_id, roles_for_scopes(scopes))
            for contest_id, scopes in json.loads(row.contest_scopes or "{}").items()
        },
        protected_master_contests=json.loads(row.protected_master_contests or "[]"),
    )


def _mail(row: MailQueueItemRow) -> MailQueueItem:
    return MailQueueItem(
        mail_queue_id=row.mail_queue_id,
        contest_id=row.contest_id,
        sent_at=_aware(row.sent_at),
        last_attempt_at=_aware(row.last_attempt_at),
        mail_type=row.mail_type,
        recipient_email=row.recipient_email,
        subject=row.subject,
        body_text=row.body_text,
        body_html=row.body_html,
        status=row.status,
        created_at=_aware(row.created_at),
    )


def _contact_inquiry(row: ContactInquiryRow) -> ContactInquiry:
    return ContactInquiry(
        contact_inquiry_id=row.contact_inquiry_id,
        title=row.title,
        sender_name=row.sender_name,
        sender_email=row.sender_email,
        body=row.body,
        status=row.status,
        answer_body=row.answer_body,
        answered_by_email=row.answered_by_email,
        answered_at=_aware(row.answered_at) if row.answered_at else None,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


class DbStore:
    def __init__(self) -> None:
        create_schema()
        if settings.enable_demo_seed:
            self.seed()
            self.ensure_demo_fixtures()
            from app.services.contest_ownership import backfill_contest_owners
            with self._session() as db:
                backfill_contest_owners(db.connection())
                db.commit()
        self.ensure_bootstrap_service_master()

    def _session(self) -> Session:
        return current_session(SessionLocal)

    def is_contest_deleted(self, contest_id: str) -> bool:
        with self._session() as db:
            return db.scalar(select(ContestRow.deleted_at).where(ContestRow.contest_id == contest_id)
                .execution_options(include_deleted_contests=True)) is not None

    def get_contest(self, contest_id: str) -> Contest | None:
        """Refresh only this contest; unrelated contests are never scanned."""
        with self._session() as db:
            found = db.execute(select(ContestRow, select(ScoreboardReleaseRow.division_id).where(
                ScoreboardReleaseRow.contest_id == contest_id,
                ScoreboardReleaseRow.mode != "not_started").exists())
                .where(ContestRow.contest_id == contest_id)).first()
            if not found:
                return None
            row, locked = found
            status = _schedule_status(row.status, row.start_at, row.end_at, now_utc())
            if status != row.status:
                row.status = status
                db.commit()
            db.info.setdefault("request_contest_policies", {})[contest_id] = row
            return _contest(row, release_locked=locked)

    def get_problem(self, problem_id: str) -> Problem | None:
        with self._session() as db:
            row = db.get(ProblemRow, problem_id)
            return _problem(row) if row else None

    def contest_problems(self, contest_id: str, division_id: str) -> list[Problem]:
        with self._session() as db:
            rows = db.scalars(select(ProblemRow).where(ProblemRow.contest_id == contest_id,
                ProblemRow.division_id == division_id).order_by(ProblemRow.display_order)).all()
            return [_problem(row) for row in rows]

    def participant_solve_statuses(self, contest_id: str, team_id: str) -> dict[str, str]:
        with self._session() as db:
            pairs = db.execute(select(SubmissionRow.problem_id, SubmissionRow.status).where(
                SubmissionRow.contest_id == contest_id, SubmissionRow.participant_team_id == team_id,
                SubmissionRow.submission_kind == "participant").distinct()).all()
        statuses = {}
        for pid, status in pairs:
            if status == "accepted":
                statuses[pid] = "accepted"
            elif status not in {"waiting", "preparing", "judging"} and statuses.get(pid) != "accepted":
                statuses[pid] = "wrong"
        return statuses

    @staticmethod
    def _touch_session(db: Session, session) -> None:
        """Write activity at most once per interval; never cache authorization."""
        cutoff = now_utc() - timedelta(seconds=settings.session_touch_interval_seconds)
        if session.last_seen_at is not None and _aware(session.last_seen_at) > cutoff:
            return
        model = type(session)
        pk = inspect(model).primary_key[0]
        db.execute(update(model).where(pk == getattr(session, pk.key), model.revoked_at.is_(None),
            or_(model.last_seen_at.is_(None), model.last_seen_at <= cutoff))
            .values(last_seen_at=now_utc()).execution_options(synchronize_session=False))
        db.commit()

    @property
    def contests(self) -> dict[str, Contest]:
        self.refresh_contest_statuses()
        with self._session() as db:
            rows = db.scalars(select(ContestRow)).all()
            return {row.contest_id: _contest(row) for row in rows}

    def refresh_contest_statuses(self) -> None:
        now = now_utc()
        with self._session() as db:
            rows = db.scalars(select(ContestRow)).all()
            changed = False
            for row in rows:
                status = row.status
                start_at = _aware(row.start_at)
                end_at = _aware(row.end_at)
                next_status = _schedule_status(status, start_at, end_at, now)
                if next_status != status:
                    row.status = next_status
                    changed = True
            if changed:
                db.commit()

    @property
    def divisions(self) -> dict[str, ContestDivision]:
        with self._session() as db:
            rows = db.scalars(select(ContestDivisionRow)).all()
            return {row.division_id: _division(row) for row in rows}

    @property
    def problems(self) -> dict[str, Problem]:
        with self._session() as db:
            rows = db.scalars(select(ProblemRow)).all()
            return {row.problem_id: _problem(row) for row in rows}

    @property
    def teams(self) -> dict[str, ParticipantTeam]:
        with self._session() as db:
            rows = db.scalars(select(ParticipantTeamRow).options(selectinload(ParticipantTeamRow.members))).all()
            return {row.participant_team_id: _team(row) for row in rows}

    @property
    def submissions(self) -> dict[str, Submission]:
        with self._session() as db:
            rows = db.scalars(select(SubmissionRow)).all()
            titles = _submission_staff_titles(rows, db)
            return {row.submission_id: _submission(row, staff_titles=titles) for row in rows}

    def get_submission(self, submission_id: str, *, include_source: bool = True) -> Submission | None:
        base = select(SubmissionRow).where(SubmissionRow.submission_id == submission_id, SubmissionRow.submission_kind != "verification_trial")
        if not include_source:
            base = base.options(
                load_only(
                    SubmissionRow.submission_id,
                    SubmissionRow.contest_id,
                    SubmissionRow.division_id,
                    SubmissionRow.problem_id,
                    SubmissionRow.participant_team_id,
                    SubmissionRow.team_member_id,
                    SubmissionRow.submission_kind,
                    SubmissionRow.submitted_by_name,
                    SubmissionRow.submitted_by_email,
                    SubmissionRow.language,
                    SubmissionRow.status,
                    SubmissionRow.submitted_at,
                    SubmissionRow.status_updated_at,
                    SubmissionRow.compile_message,
                    SubmissionRow.judge_message,
                    SubmissionRow.failed_testcase_order,
                    SubmissionRow.progress_current,
                    SubmissionRow.progress_total,
                    SubmissionRow.runtime_ms,
                    SubmissionRow.memory_kb,
                )
            )
        with self._session() as db:
            row = db.scalar(base)
            return _submission(row, include_source=include_source, staff_titles=_submission_staff_titles([row], db)) if row else None

    def contests_by_ids(self, contest_ids: list[str]) -> dict[str, Contest]:
        ids = list(dict.fromkeys(contest_ids))
        if not ids:
            return {}
        with self._session() as db:
            rows = db.scalars(select(ContestRow).where(ContestRow.contest_id.in_(ids))).all()
            return {row.contest_id: _contest(row) for row in rows}

    def divisions_by_ids(self, division_ids: list[str]) -> dict[str, ContestDivision]:
        ids = list(dict.fromkeys(division_ids))
        if not ids:
            return {}
        with self._session() as db:
            rows = db.scalars(select(ContestDivisionRow).where(ContestDivisionRow.division_id.in_(ids))).all()
            return {row.division_id: _division(row) for row in rows}

    def problems_by_ids(self, problem_ids: list[str]) -> dict[str, Problem]:
        ids = list(dict.fromkeys(problem_ids))
        if not ids:
            return {}
        with self._session() as db:
            rows = db.scalars(select(ProblemRow).where(ProblemRow.problem_id.in_(ids))).all()
            return {row.problem_id: _problem(row) for row in rows}

    def teams_by_ids(self, participant_team_ids: list[str | None]) -> dict[str, ParticipantTeam]:
        ids = [item for item in dict.fromkeys(participant_team_ids) if item]
        if not ids:
            return {}
        with self._session() as db:
            rows = db.scalars(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id.in_(ids))
            ).all()
            return {row.participant_team_id: _team(row) for row in rows}

    def participant_teams_for_operator(self, contest_id: str) -> list[ParticipantTeam]:
        with self._session() as db:
            teams = db.scalars(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(
                    ParticipantTeamRow.contest_id == contest_id,
                    ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
                )
                .order_by(ParticipantTeamRow.team_name)
            ).all()
            member_ids = [
                member.team_member_id
                for team in teams
                for member in team.members
            ]
            session_stats: dict[str, tuple[int, datetime | None]] = {}
            if member_ids:
                rows = db.execute(
                    select(
                        TeamSessionRow.team_member_id,
                        func.count(TeamSessionRow.team_session_id),
                        func.max(func.coalesce(TeamSessionRow.last_seen_at, TeamSessionRow.issued_at)),
                    )
                    .where(
                        TeamSessionRow.team_member_id.in_(member_ids),
                        TeamSessionRow.revoked_at.is_(None),
                        TeamSessionRow.expires_at > now_utc(),
                    )
                    .group_by(TeamSessionRow.team_member_id)
                ).all()
                session_stats = {
                    str(member_id): (int(count or 0), _aware(last_seen_at))
                    for member_id, count, last_seen_at in rows
                }

            result: list[ParticipantTeam] = []
            for team in teams:
                item = _team(team)
                for member in item.members:
                    count, last_seen_at = session_stats.get(
                        member.team_member_id,
                        (0, None),
                    )
                    member.active_sessions = count
                    member.last_session_seen_at = last_seen_at
                result.append(item)
            return result

    def judge_nodes_by_ids(self, judge_node_ids: list[str]) -> dict[str, JudgeNode]:
        ids = list(dict.fromkeys(judge_node_ids))
        if not ids:
            return {}
        with self._session() as db:
            rows = db.scalars(select(JudgeNodeRow).where(JudgeNodeRow.judge_node_id.in_(ids))).all()
            return {row.judge_node_id: _node(row) for row in rows}

    def list_judge_agent_logs(
        self,
        node_id: str,
        *,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[JudgeAgentLog], str | None, int] | None:
        safe_limit = max(1, min(limit, 500))
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError:
            offset = 0
        with self._session() as db:
            node = db.get(JudgeNodeRow, node_id)
            if not node:
                return None
            count_stmt = select(func.count()).select_from(JudgeAgentLogRow).where(
                JudgeAgentLogRow.judge_node_id == node_id
            )
            total_count = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(
                select(JudgeAgentLogRow)
                .where(JudgeAgentLogRow.judge_node_id == node_id)
                .order_by(JudgeAgentLogRow.created_at.desc(), JudgeAgentLogRow.judge_agent_log_id.desc())
                .offset(offset)
                .limit(safe_limit)
            ).all()
            next_offset = offset + safe_limit
            next_cursor = str(next_offset) if next_offset < total_count else None
            return [_agent_log(row) for row in rows], next_cursor, total_count

    def append_operational_audit_log(
        self,
        *,
        scope: str,
        action: str,
        method: str,
        path: str,
        status_code: int,
        actor_email: str | None = None,
        actor_name: str | None = None,
        actor_role: str | None = None,
        contest_id: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        details: dict | None = None,
    ) -> OperationalAuditLog:
        row = OperationalAuditLogRow(
            scope=scope,
            action=action,
            method=method.upper(),
            path=path,
            status_code=status_code,
            actor_email=actor_email,
            actor_name=actor_name,
            actor_role=actor_role,
            contest_id=contest_id,
            client_ip=client_ip,
            user_agent=user_agent,
            request_id=request_id,
            details=json.dumps(details or {}, ensure_ascii=False, default=str),
            created_at=now_utc(),
        )
        with self._session() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            return _audit_log(row)

    def list_operational_audit_logs(
        self,
        *,
        scope: str | None = None,
        contest_id: str | None = None,
        actor_email: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[OperationalAuditLog], str | None, int]:
        safe_limit = max(1, min(limit, 300))
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError:
            offset = 0
        filters = []
        if scope:
            filters.append(OperationalAuditLogRow.scope == scope)
        if contest_id:
            filters.append(OperationalAuditLogRow.contest_id == contest_id)
        if actor_email:
            filters.append(OperationalAuditLogRow.actor_email == actor_email.strip().lower())

        base = select(OperationalAuditLogRow)
        count_stmt = select(func.count()).select_from(OperationalAuditLogRow)
        if filters:
            base = base.where(*filters)
            count_stmt = count_stmt.where(*filters)
        with self._session() as db:
            total_count = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(
                base.order_by(OperationalAuditLogRow.created_at.desc(), OperationalAuditLogRow.operational_audit_log_id.desc())
                .offset(offset)
                .limit(safe_limit)
            ).all()
            contest_ids = sorted({row.contest_id for row in rows if row.contest_id})
            contest_titles = {}
            if contest_ids:
                contest_rows = db.scalars(
                    select(ContestRow).where(ContestRow.contest_id.in_(contest_ids))
                ).all()
                contest_titles = {row.contest_id: row.title for row in contest_rows}
            next_offset = offset + safe_limit
            next_cursor = str(next_offset) if next_offset < total_count else None
            logs = [_audit_log_with_contest(row, contest_titles) for row in rows]
            from app.services.audit_context import enrich_current_problem_labels
            enrich_current_problem_labels(db, logs)
            return logs, next_cursor, total_count

    def append_access_log(
        self,
        *,
        event_type: str,
        account_scope: str,
        email: str | None = None,
        display_name: str | None = None,
        contest_id: str | None = None,
        contest_title: str | None = None,
        participant_team_id: str | None = None,
        team_name: str | None = None,
        team_member_id: str | None = None,
        member_name: str | None = None,
        actor_role: str | None = None,
        session_id: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        details: dict | None = None,
    ) -> AccessLog:
        row = AccessLogRow(
            event_type=event_type,
            account_scope=account_scope,
            email=email.strip().lower() if email else None,
            display_name=display_name,
            contest_id=contest_id,
            contest_title=contest_title,
            participant_team_id=participant_team_id,
            team_name=team_name,
            team_member_id=team_member_id,
            member_name=member_name,
            actor_role=actor_role,
            session_id=session_id,
            client_ip=client_ip,
            user_agent=user_agent,
            request_id=request_id,
            details=json.dumps(details or {}, ensure_ascii=False, default=str),
            created_at=now_utc(),
        )
        with self._session() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            return _access_log(row)

    def list_access_logs(
        self,
        *,
        account_scope: str | None = None,
        contest_id: str | None = None,
        email: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[AccessLog], str | None, int]:
        safe_limit = max(1, min(limit, 300))
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError:
            offset = 0
        filters = []
        if account_scope:
            filters.append(AccessLogRow.account_scope == account_scope)
        if contest_id:
            filters.append(AccessLogRow.contest_id == contest_id)
        if email:
            filters.append(AccessLogRow.email == email.strip().lower())

        base = select(AccessLogRow)
        count_stmt = select(func.count()).select_from(AccessLogRow)
        if filters:
            base = base.where(*filters)
            count_stmt = count_stmt.where(*filters)
        with self._session() as db:
            total_count = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(
                base.order_by(AccessLogRow.created_at.desc(), AccessLogRow.access_log_id.desc())
                .offset(offset)
                .limit(safe_limit)
            ).all()
            next_offset = offset + safe_limit
            next_cursor = str(next_offset) if next_offset < total_count else None
            return [_access_log(row) for row in rows], next_cursor, total_count

    def access_log_stats(self, *, contest_id: str | None = None) -> dict:
        return cached_result("access-stats", contest_id, settings.access_stats_cache_ttl_seconds,
                             lambda: self._access_log_stats_uncached(contest_id=contest_id))

    def _access_log_stats_uncached(self, *, contest_id: str | None = None) -> dict:
        since = now_utc() - timedelta(hours=24)
        filters = [AccessLogRow.created_at >= since]
        if contest_id:
            filters.append(AccessLogRow.contest_id == contest_id)

        with self._session() as db:
            total_count, success_count, failed_count, conflict_count, unique_account_count = db.execute(
                select(
                    func.count(),
                    func.count().filter(AccessLogRow.event_type.in_([
                        "general_login", "participant_login", "participant_session_issued",
                        "general_refresh", "participant_session_check", "operator_access",
                    ])),
                    func.count().filter(AccessLogRow.event_type == "login_failed"),
                    func.count().filter(AccessLogRow.event_type == "session_conflict"),
                    func.count(func.distinct(AccessLogRow.email)),
                ).select_from(AccessLogRow).where(*filters)
            ).one()
            active_filters = [
                TeamSessionRow.revoked_at.is_(None),
                TeamSessionRow.expires_at > now_utc(),
            ]
            if contest_id:
                active_filters.append(TeamSessionRow.contest_id == contest_id)
                active_session_count = int(
                    db.scalar(select(func.count()).select_from(TeamSessionRow).where(*active_filters)) or 0
                )
            else:
                active_team_count = int(
                    db.scalar(select(func.count()).select_from(TeamSessionRow).where(*active_filters)) or 0
                )
                active_general_count = int(
                    db.scalar(
                        select(func.count()).select_from(GeneralSessionRow).where(
                            GeneralSessionRow.revoked_at.is_(None),
                            GeneralSessionRow.refresh_expires_at > now_utc(),
                        )
                    )
                    or 0
                )
                active_session_count = active_team_count + active_general_count
            return {
                "window_hours": 24,
                "total_count": total_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "conflict_count": conflict_count,
                "unique_account_count": unique_account_count,
                "active_session_count": active_session_count,
            }

    def count_submissions(self, *, contest_id: str | None = None, division_id: str | None = None) -> int:
        filters = [SubmissionRow.submission_kind.not_in(["participant_preview", "verification_trial"])]
        if contest_id:
            filters.append(SubmissionRow.contest_id == contest_id)
        if division_id:
            filters.append(SubmissionRow.division_id == division_id)
        stmt = select(func.count()).select_from(SubmissionRow)
        if filters:
            stmt = stmt.where(*filters)
        with self._session() as db:
            return int(db.scalar(stmt) or 0)

    def count_judge_jobs(self, *, contest_id: str | None = None, status: str | None = None) -> int:
        filters = []
        if contest_id:
            filters.append(JudgeJobRow.contest_id == contest_id)
        if status:
            filters.append(JudgeJobRow.status == status)
        stmt = select(func.count()).select_from(JudgeJobRow)
        if filters:
            stmt = stmt.where(*filters)
        with self._session() as db:
            return int(db.scalar(stmt) or 0)

    def list_submissions(
        self,
        *,
        contest_id: str | None = None,
        division_id: str | None = None,
        problem_id: str | None = None,
        participant_team_id: str | None = None,
        exclude_operator_tests: bool = False,
        include_participant_previews: bool = False,
        include_source: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Submission], str | None, int]:
        safe_limit = max(1, min(limit, 300))
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError:
            offset = 0
        filters = [SubmissionRow.submission_kind != "verification_trial"]
        if not include_participant_previews:
            filters.append(SubmissionRow.submission_kind != "participant_preview")
        if contest_id:
            filters.append(SubmissionRow.contest_id == contest_id)
        if division_id:
            filters.append(SubmissionRow.division_id == division_id)
        if problem_id:
            filters.append(SubmissionRow.problem_id == problem_id)
        if participant_team_id:
            filters.append(SubmissionRow.participant_team_id == participant_team_id)
        if exclude_operator_tests:
            filters.append(SubmissionRow.submission_kind == "participant")
            operator_team_ids = select(ParticipantTeamRow.participant_team_id).where(
                ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX)
            )
            if contest_id:
                operator_team_ids = operator_team_ids.where(ParticipantTeamRow.contest_id == contest_id)
            filters.append(SubmissionRow.participant_team_id.not_in(operator_team_ids))

        base = select(SubmissionRow)
        count_stmt = select(func.count()).select_from(SubmissionRow)
        if filters:
            base = base.where(*filters)
            count_stmt = count_stmt.where(*filters)
        if not include_source:
            base = base.options(
                load_only(
                    SubmissionRow.submission_id,
                    SubmissionRow.contest_id,
                    SubmissionRow.division_id,
                    SubmissionRow.problem_id,
                    SubmissionRow.participant_team_id,
                    SubmissionRow.team_member_id,
                    SubmissionRow.submission_kind,
                    SubmissionRow.submitted_by_name,
                    SubmissionRow.submitted_by_email,
                    SubmissionRow.language,
                    SubmissionRow.status,
                    SubmissionRow.submitted_at,
                    SubmissionRow.status_updated_at,
                    SubmissionRow.compile_message,
                    SubmissionRow.judge_message,
                    SubmissionRow.failed_testcase_order,
                    SubmissionRow.progress_current,
                    SubmissionRow.progress_total,
                    SubmissionRow.runtime_ms,
                    SubmissionRow.memory_kb,
                )
            )
        with self._session() as db:
            total_count = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(
                base.order_by(SubmissionRow.submitted_at.desc(), SubmissionRow.submission_id.desc())
                .offset(offset)
                .limit(safe_limit)
            ).all()
            next_offset = offset + safe_limit
            next_cursor = str(next_offset) if next_offset < total_count else None
            titles = _submission_staff_titles(rows, db)
            return [_submission(row, include_source=include_source, staff_titles=titles) for row in rows], next_cursor, total_count

    def judge_jobs_by_submission_ids(self, submission_ids: list[str]) -> dict[str, JudgeJob]:
        if not submission_ids:
            return {}
        with self._session() as db:
            rows = db.scalars(
                select(JudgeJobRow)
                .where(JudgeJobRow.submission_id.in_(submission_ids), ~rejudging.is_job())
                .order_by(JudgeJobRow.created_at.desc())
            ).all()
            jobs: dict[str, JudgeJob] = {}
            for row in rows:
                if row.submission_id not in jobs:
                    jobs[row.submission_id] = _job(row)
            return jobs

    def submission_source_lengths(self, submission_ids: list[str]) -> dict[str, int]:
        if not submission_ids:
            return {}
        with self._session() as db:
            byte_length = (func.octet_length(SubmissionRow.source_code)
                if db.bind.dialect.name == "postgresql"
                else func.length(cast(SubmissionRow.source_code, LargeBinary)))
            rows = db.execute(
                select(SubmissionRow.submission_id, byte_length)
                .where(SubmissionRow.submission_id.in_(submission_ids))
            ).all()
            return {
                str(submission_id): int(size or 0)
                for submission_id, size in rows
            }

    def pending_queue_ranks(self, contest_id: str | None = None, *, submission_ids: list[str] | None = None) -> dict[str, int]:
        if submission_ids == []:
            return {}
        filters = [JudgeJobRow.status == "pending", ~rejudging.is_job()]
        if contest_id:
            filters.append(JudgeJobRow.contest_id == contest_id)
        with self._session() as db:
            if submission_ids is not None:
                ranked = select(JudgeJobRow.submission_id,
                    (func.row_number().over(order_by=[JudgeJobRow.queue_position, JudgeJobRow.judge_job_id])-1).label("rank")
                ).where(*filters).subquery()
                return dict(db.execute(select(ranked.c.submission_id, ranked.c.rank)
                    .where(ranked.c.submission_id.in_(submission_ids))).all())
            rows = db.scalars(
                select(JudgeJobRow)
                .where(*filters)
                .order_by(JudgeJobRow.queue_position)
            ).all()
            return {row.submission_id: index for index, row in enumerate(rows, start=1)}

    def active_testcase_counts_by_problem_ids(self, problem_ids: list[str]) -> dict[str, int]:
        if not problem_ids:
            return {}
        with self._session() as db:
            rows = db.execute(
                select(TestcaseSetRow.problem_id, func.count(TestcaseRow.testcase_id))
                .join(TestcaseRow, TestcaseRow.testcase_set_id == TestcaseSetRow.testcase_set_id)
                .where(TestcaseSetRow.problem_id.in_(problem_ids), TestcaseSetRow.is_active.is_(True))
                .group_by(TestcaseSetRow.problem_id)
            ).all()
            return {str(problem_id): int(count) for problem_id, count in rows}

    @property
    def judge_jobs(self) -> dict[str, JudgeJob]:
        with self._session() as db:
            rows = db.scalars(select(JudgeJobRow)).all()
            return {row.judge_job_id: _job(row) for row in rows}

    @property
    def judge_nodes(self) -> dict[str, JudgeNode]:
        with self._session() as db:
            rows = db.scalars(select(JudgeNodeRow)).all()
            return {row.judge_node_id: _node(row) for row in rows}

    @property
    def service_notices(self) -> dict[str, ServiceNotice]:
        with self._session() as db:
            rows = db.scalars(select(ServiceNoticeRow)).all()
            return {row.service_notice_id: _notice(row) for row in rows}

    @property
    def contest_notices(self) -> dict[str, ContestNotice]:
        with self._session() as db:
            rows = db.scalars(select(ContestNoticeRow)).all()
            return {row.contest_notice_id: _contest_notice(row) for row in rows}

    @property
    def contest_questions(self) -> dict[str, ContestQuestion]:
        with self._session() as db:
            rows = db.scalars(select(ContestQuestionRow).options(selectinload(ContestQuestionRow.answers))).all()
            items = {}
            for row in rows:
                items[row.contest_question_id] = _question_for_view(row, db)
            return items

    @property
    def staff_accounts(self) -> dict[str, StaffAccount]:
        with self._session() as db:
            rows = db.scalars(select(StaffAccountRow)).all()
            contest_ids = db.scalars(select(ContestRow.contest_id)).all()
            accounts = {}
            for row in rows:
                account = _staff(row)
                if account.is_service_master:
                    account.contest_scopes = {
                        contest_id: ["contest.*"]
                        for contest_id in contest_ids
                    }
                accounts[row.staff_account_id] = account
            return accounts

    @property
    def mail_queue(self) -> dict[str, MailQueueItem]:
        with self._session() as db:
            rows = db.scalars(select(MailQueueItemRow)).all()
            return {row.mail_queue_id: _mail(row) for row in rows}

    @property
    def contact_inquiries(self) -> dict[str, ContactInquiry]:
        with self._session() as db:
            rows = db.scalars(select(ContactInquiryRow)).all()
            return {row.contact_inquiry_id: _contact_inquiry(row) for row in rows}

    @property
    def otp_codes(self) -> dict[str, str]:
        with self._session() as db:
            rows = db.scalars(select(OtpCodeRow)).all()
            return {row.email: row.code for row in rows}

    @property
    def problem_assets(self) -> dict[str, ProblemAsset]:
        with self._session() as db:
            rows = db.scalars(select(ProblemAssetRow)).all()
            return {row.asset_id: _asset(row) for row in rows}

    @property
    def testcase_sets(self) -> dict[str, TestcaseSet]:
        with self._session() as db:
            rows = db.scalars(select(TestcaseSetRow)).all()
            return {row.testcase_set_id: _testcase_set(row) for row in rows}

    @property
    def testcases(self) -> dict[str, Testcase]:
        with self._session() as db:
            rows = db.scalars(select(TestcaseRow)).all()
            return {row.testcase_id: _testcase(row) for row in rows}

    def seed(self) -> None:
        with self._session() as db:
            if db.scalar(select(func.count()).select_from(ContestRow)):
                return

            start, end, freeze = demo_times()
            contest = ContestRow(
                title="Zerone Spring Invitational",
                organization_name="Zerone",
                overview="실전 대회 운영 흐름을 검증하는 온라인 저지 데모 대회입니다.",
                status=ContestStatus.RUNNING.value,
                start_at=start,
                end_at=end,
                freeze_at=freeze,
                emergency_notice="제출 지연은 long polling 상태창에서 확인하세요.",
            )
            db.add(contest)
            db.flush()

            beginner = ContestDivisionRow(
                contest_id=contest.contest_id,
                code="beginner",
                name="Beginner",
                description="입문 유형. 문제와 스코어보드는 이 유형 안에서만 집계됩니다.",
                display_order=1,
            )
            advanced = ContestDivisionRow(
                contest_id=contest.contest_id,
                code="advanced",
                name="Advanced",
                description="심화 유형. 같은 대회명을 공유하지만 별도 대회처럼 운영됩니다.",
                display_order=2,
            )
            db.add_all([beginner, advanced])
            db.flush()

            scheduled = ContestRow(
                title="Hidden Scheduled Contest",
                organization_name="Zerone",
                overview="공개 예정 대회입니다.",
                status=ContestStatus.SCHEDULED.value,
                start_at=now_utc() + timedelta(days=7),
                end_at=now_utc() + timedelta(days=7, hours=4),
                freeze_at=now_utc() + timedelta(days=7, hours=3),
            )
            db.add(scheduled)

            for division, titles in {
                beginner.division_id: ["A+B Reloaded", "Queue Pressure", "Frozen Scoreboard"],
                advanced.division_id: ["Vector Sprint", "Shard Balancer", "Live Rank Freeze"],
            }.items():
                for index, title in enumerate(titles):
                    db.add(
                        ProblemRow(
                            contest_id=contest.contest_id,
                            division_id=division,
                            problem_code=chr(ord("A") + index),
                            title=title,
                            statement=f"{title} 문제 설명입니다. 입력을 읽고 정해진 형식으로 출력하세요.",
                            time_limit_ms=1000 + index * 500,
                            memory_limit_mb=512,
                            display_order=index + 1,
                        )
                    )

            rookie = ParticipantTeamRow(
                contest_id=contest.contest_id,
                division_id=beginner.division_id,
                team_name="Team Rookie",
                status="active",
            )
            async_team = ParticipantTeamRow(
                contest_id=contest.contest_id,
                division_id=advanced.division_id,
                team_name="Team Async",
                status="active",
            )
            db.add_all([rookie, async_team])
            db.flush()
            db.add_all(
                [
                    TeamMemberRow(
                        contest_id=contest.contest_id,
                        participant_team_id=rookie.participant_team_id,
                        role=TeamMemberRole.LEADER.value,
                        name="Test One",
                        email="test1@zoj.com",
                    ),
                    TeamMemberRow(
                        contest_id=contest.contest_id,
                        participant_team_id=async_team.participant_team_id,
                        role=TeamMemberRole.LEADER.value,
                        name="Test Two",
                        email="test2@zoj.com",
                    ),
                    TeamMemberRow(
                        contest_id=contest.contest_id,
                        participant_team_id=async_team.participant_team_id,
                        role=TeamMemberRole.MEMBER.value,
                        name="Test Two Member",
                        email="test2-member@zoj.com",
                    ),
                ]
            )
            db.flush()

            starter_problem = db.scalar(
                select(ProblemRow).where(
                    ProblemRow.contest_id == contest.contest_id,
                    ProblemRow.division_id == advanced.division_id,
                    ProblemRow.problem_code == "A",
                )
            )
            leader = db.scalar(
                select(TeamMemberRow).where(
                    TeamMemberRow.participant_team_id == async_team.participant_team_id,
                    TeamMemberRow.role == TeamMemberRole.LEADER.value,
                )
            )
            if starter_problem and leader:
                db.add(
                    SubmissionRow(
                        contest_id=contest.contest_id,
                        division_id=advanced.division_id,
                        problem_id=starter_problem.problem_id,
                        participant_team_id=async_team.participant_team_id,
                        team_member_id=leader.team_member_id,
                        language="cpp17",
                        source_code="int main(){return 0;}",
                        status=SubmissionStatus.ACCEPTED.value,
                    )
                )

            db.add(
                ServiceNoticeRow(
                    title="서비스 점검 안내",
                    summary="오늘 23시부터 10분간 점검 예정입니다.",
                    body="서비스 안정화를 위한 짧은 점검입니다.",
                    emergency=True,
                )
            )
            db.add_all(
                [
                    StaffAccountRow(
                        email="test3@zoj.com",
                        display_name="Service Master",
                        is_service_master=True,
                    ),
                    StaffAccountRow(
                        email="test4@zoj.com",
                        display_name="Contest Operator",
                        contest_scopes=json.dumps({contest.contest_id: ["contest.*"]}),
                    ),
                ]
            )
            db.commit()

    def ensure_bootstrap_service_master(self) -> None:
        email = settings.bootstrap_service_master_email
        if not email:
            return
        with self._session() as db:
            account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
            if account:
                return
            db.add(
                StaffAccountRow(
                    email=email,
                    display_name=settings.bootstrap_service_master_name,
                    is_service_master=True,
                    permissions="",
                    contest_scopes=json.dumps({}),
                )
            )
            db.commit()

    def ensure_demo_fixtures(self) -> None:
        with self._session() as db:
            contest = db.scalar(
                select(ContestRow)
                .where(
                    ContestRow.status.not_in(
                        [
                            ContestStatus.DRAFT.value,
                            ContestStatus.SCHEDULE_TBD.value,
                            ContestStatus.SCHEDULED.value,
                        ]
                    )
                )
                .order_by(ContestRow.created_at)
            )
            if not contest:
                return
            divisions = db.scalars(
                select(ContestDivisionRow).where(ContestDivisionRow.contest_id == contest.contest_id).order_by(ContestDivisionRow.display_order)
            ).all()
            if len(divisions) < 2:
                return
            beginner, advanced = divisions[0], divisions[1]

            for email, display_name, is_master, scopes in [
                ("test3@zoj.com", "Service Master", True, {}),
                ("test4@zoj.com", "Contest Operator", False, {contest.contest_id: ["contest.*"]}),
            ]:
                account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
                if not account:
                    db.add(
                        StaffAccountRow(
                            email=email,
                            display_name=display_name,
                            is_service_master=is_master,
                            contest_scopes=json.dumps(scopes),
                        )
                    )
                elif not is_master:
                    current_scopes = json.loads(account.contest_scopes or "{}")
                    for cid, grants in scopes.items():
                        current_scopes.setdefault(cid, grants)
                    account.contest_scopes = json.dumps(current_scopes)

            for email, name, team_name, division in [
                ("test1@zoj.com", "Test One", "ZOJ Test Beginner", beginner),
                ("test2@zoj.com", "Test Two", "ZOJ Test Advanced", advanced),
            ]:
                member = db.scalar(select(TeamMemberRow).where(TeamMemberRow.contest_id == contest.contest_id, TeamMemberRow.email == email))
                if member:
                    continue
                team = db.scalar(select(ParticipantTeamRow).where(ParticipantTeamRow.contest_id == contest.contest_id, ParticipantTeamRow.team_name == team_name))
                if not team:
                    team = ParticipantTeamRow(contest_id=contest.contest_id, division_id=division.division_id, team_name=team_name, status="active")
                    db.add(team)
                    db.flush()
                db.add(
                    TeamMemberRow(
                        contest_id=contest.contest_id,
                        participant_team_id=team.participant_team_id,
                        role=TeamMemberRole.LEADER.value,
                        name=name,
                        email=email,
                    )
                )
            for member in db.scalars(
                select(TeamMemberRow).where(
                    TeamMemberRow.contest_id == contest.contest_id,
                    TeamMemberRow.email.like("renamed-member-%"),
                )
            ).all():
                db.delete(member)
            for member in db.scalars(
                select(TeamMemberRow).where(
                    TeamMemberRow.contest_id == contest.contest_id,
                    TeamMemberRow.email.like("new-member-%"),
                )
            ).all():
                db.delete(member)
            db.commit()

    def is_staff_email(self, email: str) -> bool:
        with self._session() as db:
            return db.scalar(select(StaffAccountRow.staff_account_id).where(StaffAccountRow.email == email).limit(1)) is not None

    def _participant_email_conflicts(self, db: Session, emails: list[str]) -> list[str]:
        normalized = sorted({email.strip().lower() for email in emails if email.strip()})
        if not normalized:
            return []
        existing = db.scalars(
            select(TeamMemberRow.email).where(func.lower(TeamMemberRow.email).in_(normalized))
        ).all()
        return sorted({item.lower() for item in existing})

    def _contest_participant_email_conflicts(self, db: Session, contest_id: str, emails: list[str]) -> list[str]:
        normalized = sorted({email.strip().lower() for email in emails if email.strip()})
        if not normalized:
            return []
        existing = db.scalars(
            select(TeamMemberRow.email).where(
                TeamMemberRow.contest_id == contest_id,
                func.lower(TeamMemberRow.email).in_(normalized),
            )
        ).all()
        return sorted({item.lower() for item in existing})

    def _staff_email_conflicts(self, db: Session, emails: list[str]) -> list[str]:
        normalized = sorted({email.strip().lower() for email in emails if email.strip()})
        if not normalized:
            return []
        existing = db.scalars(
            select(StaffAccountRow.email).where(func.lower(StaffAccountRow.email).in_(normalized))
        ).all()
        return sorted({item.lower() for item in existing})

    def _contest_staff_email_conflicts(self, db: Session, contest_id: str, emails: list[str]) -> list[str]:
        normalized = sorted({email.strip().lower() for email in emails if email.strip()})
        if not normalized:
            return []
        rows = db.scalars(
            select(StaffAccountRow).where(
                or_(
                    StaffAccountRow.is_service_master.is_(True),
                    func.lower(StaffAccountRow.email).in_(normalized),
                )
            )
        ).all()
        conflicts = []
        for row in rows:
            email = row.email.lower()
            if email not in normalized:
                continue
            scopes = json.loads(row.contest_scopes or "{}")
            if row.is_service_master or scopes.get(contest_id):
                conflicts.append(email)
        return sorted(set(conflicts))

    def _issue_staff_session(self, db: Session, account: StaffAccountRow) -> dict:
        access_token = new_session_token(
            "staff_access",
            account.staff_account_id,
            settings.staff_access_token_ttl_seconds,
            {"email": account.email, "service_master": account.is_service_master},
        )
        refresh_token = new_session_token("staff_refresh", account.staff_account_id, settings.staff_refresh_token_ttl_seconds)
        issued_at = now_utc()
        session = StaffSessionRow(
            staff_account_id=account.staff_account_id,
            access_token_hash=token_hash(access_token),
            refresh_token_hash=token_hash(refresh_token),
            issued_at=issued_at,
            access_expires_at=issued_at + timedelta(seconds=settings.staff_access_token_ttl_seconds),
            refresh_expires_at=issued_at + timedelta(seconds=settings.staff_refresh_token_ttl_seconds),
            last_seen_at=issued_at,
        )
        db.add(session)
        db.commit()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "staff": _staff(account).model_dump(mode="json"),
            "default_redirect": "/admin" if account.is_service_master else "/operator",
        }

    def create_staff_otp(self, email: str) -> str | None:
        with self._session() as db:
            account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
            if not account:
                return None
            code = f"{secrets.randbelow(1_000_000):06d}"
            row = db.get(OtpCodeRow, email)
            if row:
                row.contest_id = STAFF_OTP_SCOPE
                row.code = code
                row.created_at = now_utc()
                row.expires_at = now_utc() + timedelta(seconds=settings.otp_ttl_seconds)
                row.verified_at = None
            else:
                db.add(
                    OtpCodeRow(
                        email=email,
                        contest_id=STAFF_OTP_SCOPE,
                        code=code,
                        expires_at=now_utc() + timedelta(seconds=settings.otp_ttl_seconds),
                    )
                )
            db.commit()
            content = login_verification_mail(code=code, ttl_seconds=settings.otp_ttl_seconds, audience="staff")
            self.enqueue_mail(
                mail_type="staff_otp",
                recipient_email=email,
                subject=content.subject,
                body_text=content.body_text,
                body_html=content.body_html,
            )
            return code

    def staff_otp_retry_after_seconds(self, email: str) -> int:
        with self._session() as db:
            row = db.get(OtpCodeRow, email)
            return self._otp_retry_after_seconds(row, STAFF_OTP_SCOPE)

    def verify_staff_otp(self, email: str, otp_code: str) -> dict | None:
        with self._session() as db:
            otp = db.get(OtpCodeRow, email)
            if not otp or otp.contest_id != STAFF_OTP_SCOPE or otp.code != otp_code or _aware(otp.expires_at) <= now_utc():
                return None
            otp.verified_at = now_utc()
            account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
            if not account:
                return None
            return self._issue_staff_session(db, account)

    def _has_general_login_identity(self, db: Session, email: str) -> bool:
        if is_internal_mail_recipient(email):
            return False
        from app.orm_models import IdentityAccountRow
        if db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.email == email.lower())):
            return True
        account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
        if account:
            return True
        member = db.scalar(
            select(TeamMemberRow.team_member_id)
            .join(
                ParticipantTeamRow,
                TeamMemberRow.participant_team_id == ParticipantTeamRow.participant_team_id,
            )
            .where(
                TeamMemberRow.email == email,
                ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
            )
            .limit(1)
        )
        return member is not None

    def create_general_otp(self, email: str, allow_new: bool = False) -> str | None:
        with self._session() as db:
            if is_internal_mail_recipient(email) or (not allow_new and not self._has_general_login_identity(db, email)):
                return None
            code = f"{secrets.randbelow(1_000_000):06d}"
            row = db.get(OtpCodeRow, email)
            if row:
                row.contest_id = GENERAL_OTP_SCOPE
                row.code = code
                row.created_at = now_utc()
                row.expires_at = now_utc() + timedelta(seconds=settings.otp_ttl_seconds)
                row.verified_at = None
            else:
                db.add(
                    OtpCodeRow(
                        email=email,
                        contest_id=GENERAL_OTP_SCOPE,
                        code=code,
                        expires_at=now_utc() + timedelta(seconds=settings.otp_ttl_seconds),
                    )
                )
            db.commit()
            content = login_verification_mail(code=code, ttl_seconds=settings.otp_ttl_seconds, audience="general")
            self.enqueue_mail(
                mail_type="general_otp",
                recipient_email=email,
                subject=content.subject,
                body_text=content.body_text,
                body_html=content.body_html,
            )
            return code

    def general_otp_retry_after_seconds(self, email: str) -> int:
        with self._session() as db:
            row = db.get(OtpCodeRow, email)
            return self._otp_retry_after_seconds(row, GENERAL_OTP_SCOPE)

    def _general_profile(self, db: Session, email: str, issue_operator_session: bool = False) -> dict | None:
        from app.orm_models import IdentityAccountRow
        identity = db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.email == email.lower()))
        participant_contests = []
        account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
        members = db.scalars(
            select(TeamMemberRow)
            .where(func.lower(TeamMemberRow.email) == email.lower())
            .order_by(TeamMemberRow.name)
        ).all()
        display_name = ""
        for member in members:
            team = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == member.participant_team_id)
            )
            contest = db.get(ContestRow, member.contest_id)
            division = db.get(ContestDivisionRow, team.division_id) if team else None
            if not team or not contest or not division:
                continue
            team_model = _team(team)
            member_model = next((item for item in team_model.members if item.team_member_id == member.team_member_id), None)
            if not member_model:
                continue
            display_name = display_name or member_model.name
            if contest_hidden_by_status(contest):
                continue
            participant_contests.append(
                {
                    "contest": _contest(contest).model_dump(mode="json"),
                    "team": team_model.model_dump(mode="json"),
                    "member": member_model.model_dump(mode="json"),
                    "division": _division(division).model_dump(mode="json"),
                }
            )

        operator_contests = []
        operator_session = None
        if account:
            display_name = account.display_name or display_name
            scopes = json.loads(account.contest_scopes or "{}")
            for contest_id, scope_list in sorted(scopes.items()):
                if not scope_list:
                    continue
                contest = db.get(ContestRow, contest_id)
                if not contest:
                    continue
                operator_contests.append({"contest": _contest(contest).model_dump(mode="json"), "scopes": sorted(set(scope_list))})
            if account.is_service_master:
                for contest in db.scalars(select(ContestRow).order_by(ContestRow.start_at)).all():
                    if any(item["contest"]["contest_id"] == contest.contest_id for item in operator_contests):
                        continue
                    operator_contests.append({"contest": _contest(contest).model_dump(mode="json"), "scopes": ["master"]})
            if issue_operator_session:
                operator_session = {
                    "access_token": "",
                    "refresh_token": "",
                    "staff": _staff(account).model_dump(mode="json"),
                    "default_redirect": "/admin" if account.is_service_master else "/operator",
                }

        if not members and not operator_contests and not account and not identity:
            return None
        return {
            "account": {"email": email, "display_name": identity.display_name if identity else display_name or email, "account_id": identity.account_id if identity else None},
            "participant_contests": sorted(participant_contests, key=lambda item: item["contest"]["start_at"]),
            "operator_contests": sorted(operator_contests, key=lambda item: item["contest"]["start_at"]),
            "operator_session": operator_session,
        }

    def _issue_general_session(self, db: Session, email: str, profile: dict) -> dict:
        from app.orm_models import IdentityAccountRow
        identity = db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.email == email.lower()))
        if not identity:
            identity = IdentityAccountRow(email=email.lower(),display_name=profile["account"]["display_name"])
            db.add(identity)
            db.flush()
        access_token = new_session_token("general_access", email, settings.staff_access_token_ttl_seconds)
        refresh_token = new_session_token("general_refresh", email, settings.staff_refresh_token_ttl_seconds)
        issued_at = now_utc()
        db.add(
            GeneralSessionRow(
                email=email,
                access_token_hash=token_hash(access_token),
                refresh_token_hash=token_hash(refresh_token),
                issued_at=issued_at,
                access_expires_at=issued_at + timedelta(seconds=settings.staff_access_token_ttl_seconds),
                refresh_expires_at=issued_at + timedelta(seconds=settings.staff_refresh_token_ttl_seconds),
                last_seen_at=issued_at,
            )
        )
        db.commit()
        with self._session() as fresh_db:
            fresh_profile = self._general_profile(fresh_db, email, issue_operator_session=True)
        result = {"access_token": access_token, "refresh_token": refresh_token, **(fresh_profile or profile)}
        if result.get("operator_session"):
            result["operator_session"]["access_token"] = access_token
            result["operator_session"]["refresh_token"] = refresh_token
        return result

    def _active_general_session_rows(self, db: Session, email: str) -> list[GeneralSessionRow]:
        return db.scalars(
            select(GeneralSessionRow)
            .where(
                func.lower(GeneralSessionRow.email) == email.lower(),
                GeneralSessionRow.revoked_at.is_(None),
                GeneralSessionRow.refresh_expires_at > now_utc(),
            )
            .order_by(GeneralSessionRow.issued_at.desc())
        ).all()

    def _active_team_session_rows_for_email(self, db: Session, email: str) -> list[TeamSessionRow]:
        member_ids = select(TeamMemberRow.team_member_id).where(func.lower(TeamMemberRow.email) == email.lower())
        return db.scalars(
            select(TeamSessionRow)
            .where(
                TeamSessionRow.team_member_id.in_(member_ids),
                TeamSessionRow.revoked_at.is_(None),
                TeamSessionRow.expires_at > now_utc(),
            )
            .order_by(TeamSessionRow.issued_at.desc())
        ).all()

    def _active_login_session_summary(self, db: Session, email: str) -> dict | None:
        general_sessions = self._active_general_session_rows(db, email)
        team_sessions = self._active_team_session_rows_for_email(db, email)
        active_count = len(general_sessions) + len(team_sessions)
        if active_count == 0:
            return None

        candidates = [*general_sessions, *team_sessions]
        latest = max(
            candidates,
            key=lambda row: _aware(row.last_seen_at) or _aware(row.issued_at),
        )
        latest_at = _aware(latest.last_seen_at) or _aware(latest.issued_at)
        return {
            "active_session_count": active_count,
            "last_seen_at": latest_at.isoformat() if latest_at else None,
        }

    def _revoke_active_login_sessions_for_email(self, db: Session, email: str) -> None:
        revoked_at = now_utc()
        sessions = db.scalars(
            select(GeneralSessionRow)
            .where(func.lower(GeneralSessionRow.email) == email.lower(), GeneralSessionRow.revoked_at.is_(None))
            .order_by(GeneralSessionRow.general_session_id)
            .with_for_update()
        ).all()
        for session in sessions:
            session.revoked_at = revoked_at
        for session in self._active_team_session_rows_for_email(db, email):
            session.revoked_at = revoked_at
        account_ids = select(StaffAccountRow.staff_account_id).where(func.lower(StaffAccountRow.email) == email.lower())
        db.execute(
            update(StaffSessionRow)
            .where(StaffSessionRow.staff_account_id.in_(account_ids), StaffSessionRow.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        members = db.scalars(select(TeamMemberRow).where(func.lower(TeamMemberRow.email) == email.lower())).all()
        for member in members:
            member.active_sessions = 0
            member.session_revoked_at = revoked_at

    def _revoke_active_team_sessions_for_member(
        self,
        db: Session,
        member: TeamMemberRow,
        *,
        mark_revoked: bool = False,
    ) -> None:
        revoked_at = now_utc()
        sessions = db.scalars(
            select(TeamSessionRow).where(
                TeamSessionRow.team_member_id == member.team_member_id,
                TeamSessionRow.revoked_at.is_(None),
            )
        ).all()
        for session in sessions:
            session.revoked_at = revoked_at
        member.active_sessions = 0
        if mark_revoked:
            member.session_revoked_at = revoked_at

    def verify_general_otp(self, email: str, otp_code: str, force_new_session: bool = False) -> dict | None:
        with self._session() as db:
            otp = db.scalar(select(OtpCodeRow).where(OtpCodeRow.email == email).with_for_update())
            demo_bypass = settings.allow_empty_otp and otp_code == ""
            if not demo_bypass and (not otp or otp.verified_at is not None or otp.contest_id != GENERAL_OTP_SCOPE or otp.code != otp_code or _aware(otp.expires_at) <= now_utc()):
                return None
            profile = self._general_profile(db, email, issue_operator_session=False)
            if not profile and not demo_bypass:
                from app.orm_models import IdentityAccountRow
                identity = IdentityAccountRow(email=email.lower(),display_name=email.split('@')[0])
                db.add(identity)
                db.flush()
                profile = self._general_profile(db,email,issue_operator_session=False)
            if not profile:
                return None
            conflict = self._active_login_session_summary(db, email)
            if conflict and not force_new_session:
                raise SessionConflictError(conflict)
            if conflict and force_new_session:
                self._revoke_active_login_sessions_for_email(db, email)
            if otp:
                if not demo_bypass:
                    consumed = db.execute(update(OtpCodeRow).where(OtpCodeRow.email==email,OtpCodeRow.code==otp_code,OtpCodeRow.verified_at.is_(None)).values(verified_at=now_utc()))
                    if not consumed.rowcount:
                        return None
                else:
                    otp.verified_at = now_utc()
            return self._issue_general_session(db, email, profile)

    def get_general_by_access_token(self, access_token: str) -> dict | None:
        if not _valid_session_token(access_token, "general_access"):
            return None
        with self._session() as db:
            session = db.scalar(
                select(GeneralSessionRow).where(
                    GeneralSessionRow.access_token_hash == token_hash(access_token),
                    GeneralSessionRow.revoked_at.is_(None),
                )
            )
            if not session or _aware(session.access_expires_at) <= now_utc():
                return None
            profile = self._general_profile(db, session.email, issue_operator_session=True)
            self._touch_session(db, session)
            if profile and profile.get("operator_session"):
                profile["operator_session"]["access_token"] = access_token
                profile["operator_session"]["refresh_token"] = ""
            return profile

    def get_participant_by_general_access_token(self, contest_id: str, access_token: str) -> dict | None:
        if not _valid_session_token(access_token, "general_access"):
            return None
        with self._session() as db:
            session = db.scalar(
                select(GeneralSessionRow).where(
                    GeneralSessionRow.access_token_hash == token_hash(access_token),
                    GeneralSessionRow.revoked_at.is_(None),
                )
            )
            if not session or _aware(session.access_expires_at) <= now_utc():
                return None
            member = db.scalar(
                select(TeamMemberRow).join(ContestRow, ContestRow.contest_id == TeamMemberRow.contest_id).where(
                    TeamMemberRow.contest_id == contest_id,
                    func.lower(TeamMemberRow.email) == session.email.lower(),
                )
            )
            if not member:
                return None
            team = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == member.participant_team_id)
            )
            division = db.get(ContestDivisionRow, team.division_id) if team else None
            self._touch_session(db, session)
            if not team or not division:
                return None
            team_model = _team(team)
            member_model = next(
                item for item in team_model.members
                if item.team_member_id == member.team_member_id
            )
            return {
                "team": team_model,
                "member": member_model,
                "division": _division(division),
            }

    def get_staff_by_general_access_token(self, access_token: str) -> StaffAccount | None:
        if not _valid_session_token(access_token, "general_access"):
            return None
        with self._session() as db:
            session = db.scalar(
                select(GeneralSessionRow).where(
                    GeneralSessionRow.access_token_hash == token_hash(access_token),
                    GeneralSessionRow.revoked_at.is_(None),
                )
            )
            if not session or _aware(session.access_expires_at) <= now_utc():
                return None
            account = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == session.email))
            if not account:
                return None
            self._touch_session(db, session)
            return _staff(account)

    def refresh_general_session(self, refresh_token: str) -> dict | None:
        if not _valid_session_token(refresh_token, "general_refresh"):
            return None
        with self._session() as db:
            session = db.scalar(
                select(GeneralSessionRow).where(
                    GeneralSessionRow.refresh_token_hash == token_hash(refresh_token),
                    GeneralSessionRow.revoked_at.is_(None),
                )
            )
            if not session or _aware(session.refresh_expires_at) <= now_utc():
                return None
            access_token = new_session_token("general_access", session.email, settings.staff_access_token_ttl_seconds)
            session.access_token_hash = token_hash(access_token)
            session.access_expires_at = now_utc() + timedelta(seconds=settings.staff_access_token_ttl_seconds)
            session.last_seen_at = now_utc()
            profile = self._general_profile(db, session.email, issue_operator_session=True)
            db.commit()
            if not profile:
                return None
            result = {"access_token": access_token, "refresh_token": refresh_token, **profile}
            if result.get("operator_session"):
                result["operator_session"]["access_token"] = access_token
                result["operator_session"]["refresh_token"] = refresh_token
            return result

    def revoke_general_session(self, access_token: str | None, refresh_token: str | None) -> bool:
        with self._session() as db:
            filters = []
            if access_token:
                filters.append(GeneralSessionRow.access_token_hash == token_hash(access_token))
            if refresh_token:
                filters.append(GeneralSessionRow.refresh_token_hash == token_hash(refresh_token))
            if not filters:
                return False
            session = db.scalar(select(GeneralSessionRow).where(or_(*filters)))
            if not session:
                return False
            session.revoked_at = now_utc()
            db.commit()
            return True

    def issue_participant_session_for_general(
        self,
        email: str,
        contest_id: str,
        parent_access_token: str | None = None,
    ) -> tuple[ParticipantTeam, TeamMember, ContestDivision, str] | None:
        with self._session() as db:
            member = db.scalar(select(TeamMemberRow).join(ContestRow, ContestRow.contest_id == TeamMemberRow.contest_id).where(TeamMemberRow.contest_id == contest_id, func.lower(TeamMemberRow.email) == email.lower()))
            if not member:
                return None
            parent_session = None
            if parent_access_token:
                parent_session = db.scalar(
                    select(GeneralSessionRow).where(
                        GeneralSessionRow.access_token_hash == token_hash(parent_access_token),
                        GeneralSessionRow.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
                if not parent_session:
                    return None
            if (
                parent_session
                and member.session_revoked_at
                and _aware(parent_session.issued_at) <= _aware(member.session_revoked_at)
            ):
                return None
            team = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == member.participant_team_id)
            )
            if not team:
                return None
            division = db.get(ContestDivisionRow, team.division_id)
            if not division:
                return None
            self._revoke_active_team_sessions_for_member(db, member)
            member.active_sessions = 1
            member.last_login_at = now_utc()
            team.status = "active"
            access_token = new_session_token(
                "participant_access",
                member.team_member_id,
                settings.participant_access_token_ttl_seconds,
                {"contest_id": contest_id, "team_id": team.participant_team_id, "division_id": team.division_id},
            )
            db.add(
                TeamSessionRow(
                    contest_id=contest_id,
                    division_id=team.division_id,
                    participant_team_id=team.participant_team_id,
                    team_member_id=member.team_member_id,
                    access_token_hash=token_hash(access_token),
                    issued_at=now_utc(),
                    expires_at=now_utc() + timedelta(seconds=settings.participant_access_token_ttl_seconds),
                )
            )
            db.commit()
            db.refresh(member)
            db.refresh(team)
            team_model = _team(team)
            member_model = next(item for item in team_model.members if item.team_member_id == member.team_member_id)
            return team_model, member_model, _division(division), access_token

    def get_staff_by_access_token(self, access_token: str) -> StaffAccount | None:
        if not _valid_session_token(access_token, "staff_access"):
            return None
        with self._session() as db:
            session = db.scalar(
                select(StaffSessionRow).where(
                    StaffSessionRow.access_token_hash == token_hash(access_token),
                    StaffSessionRow.revoked_at.is_(None),
                )
            )
            if not session or _aware(session.access_expires_at) <= now_utc():
                return None
            account = db.get(StaffAccountRow, session.staff_account_id)
            self._touch_session(db, session)
            return _staff(account) if account else None

    def get_participant_by_access_token(self, contest_id: str, access_token: str) -> dict | None:
        if not _valid_session_token(access_token, "participant_access"):
            from app.services.participant_preview import get_preview_participant
            return get_preview_participant(contest_id, access_token)
        with self._session() as db:
            found = db.execute(
                select(TeamSessionRow, ParticipantTeamRow, TeamMemberRow, ContestDivisionRow)
                .join(ParticipantTeamRow, ParticipantTeamRow.participant_team_id == TeamSessionRow.participant_team_id)
                .join(TeamMemberRow, TeamMemberRow.team_member_id == TeamSessionRow.team_member_id)
                .join(ContestDivisionRow, ContestDivisionRow.division_id == TeamSessionRow.division_id)
                .join(ContestRow, ContestRow.contest_id == TeamSessionRow.contest_id)
                .options(joinedload(ParticipantTeamRow.members))
                .where(
                    TeamSessionRow.contest_id == contest_id,
                    TeamSessionRow.access_token_hash == token_hash(access_token),
                    TeamSessionRow.revoked_at.is_(None),
                )
            ).unique().one_or_none()
            if not found:
                return None
            session, team, member, division = found
            if _aware(session.expires_at) <= now_utc():
                return None
            if member.session_revoked_at and _aware(session.issued_at) <= _aware(member.session_revoked_at):
                return None
            self._touch_session(db, session)
            db.info.setdefault("request_divisions", {})[division.division_id] = division
            team_model = _team(team)
            member_model = next(item for item in team_model.members if item.team_member_id == member.team_member_id)
            return {
                "team": team_model,
                "member": member_model,
                "division": _division(division),
            }

    def refresh_staff_session(self, refresh_token: str) -> dict | None:
        if not _valid_session_token(refresh_token, "staff_refresh"):
            return None
        with self._session() as db:
            session = db.scalar(
                select(StaffSessionRow).where(
                    StaffSessionRow.refresh_token_hash == token_hash(refresh_token),
                    StaffSessionRow.revoked_at.is_(None),
                )
            )
            if not session or _aware(session.refresh_expires_at) <= now_utc():
                return None
            access_token = new_session_token("staff_access", session.staff_account_id, settings.staff_access_token_ttl_seconds)
            session.access_token_hash = token_hash(access_token)
            session.access_expires_at = now_utc() + timedelta(seconds=settings.staff_access_token_ttl_seconds)
            session.last_seen_at = now_utc()
            account = db.get(StaffAccountRow, session.staff_account_id)
            if not account:
                return None
            db.commit()
            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "staff": _staff(account).model_dump(mode="json"),
                "default_redirect": "/admin" if account.is_service_master else "/operator",
            }

    def revoke_staff_session(self, access_token: str | None, refresh_token: str | None) -> bool:
        with self._session() as db:
            filters = []
            if access_token:
                filters.append(StaffSessionRow.access_token_hash == token_hash(access_token))
            if refresh_token:
                filters.append(StaffSessionRow.refresh_token_hash == token_hash(refresh_token))
            if not filters:
                return False
            session = db.scalar(select(StaffSessionRow).where(or_(*filters)))
            if not session:
                return False
            session.revoked_at = now_utc()
            db.commit()
            return True

    def visible_public_contests(self, visible_private_ids: set[str] | None = None) -> list[Contest]:
        self.refresh_contest_statuses()
        with self._session() as db:
            rows = db.scalars(
                select(ContestRow).where(
                    ContestRow.status.not_in(
                        [
                            ContestStatus.DRAFT.value,
                            ContestStatus.SCHEDULE_TBD.value,
                            ContestStatus.SCHEDULED.value,
                        ]
                    )
                )
            ).all()
            contests = [_contest(row) for row in rows]
            return [contest for contest in contests if contest_is_public(contest) or contest.contest_id in (visible_private_ids or set())]

    def get_public_contest(self, contest_id: str, *, allow_private: bool = False) -> Contest | None:
        contest = self.get_contest(contest_id)
        if not contest or contest.status.value in {
                ContestStatus.DRAFT.value,
                ContestStatus.SCHEDULE_TBD.value,
                ContestStatus.SCHEDULED.value,
            }:
            return None
        return contest if allow_private or contest_is_public(contest) else None

    def contest_participation_counts(self, contest_id: str) -> dict[str, int]:
        """Return aggregate registration counts without loading participant data."""
        with self._session() as db:
            team_count, participant_count = db.execute(
                select(
                    func.count(func.distinct(ParticipantTeamRow.participant_team_id)),
                    func.count(TeamMemberRow.team_member_id),
                )
                .select_from(ParticipantTeamRow)
                .outerjoin(TeamMemberRow, TeamMemberRow.participant_team_id == ParticipantTeamRow.participant_team_id)
                .where(
                    ParticipantTeamRow.contest_id == contest_id,
                    ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
                )
            ).one()
            return {"participant_count": participant_count, "team_count": team_count}

    def create_contest(
        self,
        title: str | None,
        organization_name: str,
        overview: str | None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        freeze_at: datetime | None = None,
        status: ContestStatus = ContestStatus.DRAFT,
        visibility: ContestVisibility = ContestVisibility.PUBLIC,
        visibility_after_end: ContestVisibility = ContestVisibility.PUBLIC,
    ) -> Contest:
        default_start, default_end, default_freeze = demo_times()
        resolved_start = start_at or default_start
        resolved_end = end_at or (resolved_start + timedelta(hours=4))
        resolved_freeze = freeze_at or (resolved_end - timedelta(hours=1))
        with self._session() as db:
            row = ContestRow(
                title=title or f"{organization_name} Contest",
                organization_name=organization_name,
                overview=overview or f"{organization_name}에서 주최하는 대회입니다.",
                status=status.value,
                visibility=visibility.value,
                visibility_after_end=visibility_after_end.value,
                notice_access_after_end="participants" if visibility_after_end == ContestVisibility.PRIVATE else "public",
                start_at=resolved_start,
                end_at=resolved_end,
                freeze_at=resolved_freeze,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _contest(row)

    def create_contest_division(self, contest_id: str, code: str, name: str, description: str = "", display_order: int = 1) -> ContestDivision:
        with self._session() as db:
            contest = db.get(ContestRow, contest_id)
            if not contest:
                raise ValueError("contest not found")
            existing = db.scalar(select(ContestDivisionRow).where(ContestDivisionRow.contest_id == contest_id, ContestDivisionRow.code == code))
            if existing:
                raise ValueError("division code already exists")
            same_name = db.scalar(select(ContestDivisionRow).where(ContestDivisionRow.contest_id == contest_id, ContestDivisionRow.name == name))
            if same_name:
                raise ValueError("division name already exists")
            row = ContestDivisionRow(
                contest_id=contest_id,
                code=code,
                name=name,
                description=description,
                display_order=display_order,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _division(row)

    def update_contest_division(
        self,
        contest_id: str,
        division_id: str,
        code: str | None = None,
        name: str | None = None,
        description: str | None = None,
        display_order: int | None = None,
    ) -> ContestDivision | None:
        with self._session() as db:
            row = db.get(ContestDivisionRow, division_id)
            if not row or row.contest_id != contest_id:
                return None
            if code is not None and code != row.code:
                existing = db.scalar(
                    select(ContestDivisionRow).where(
                        ContestDivisionRow.contest_id == contest_id,
                        ContestDivisionRow.code == code,
                        ContestDivisionRow.division_id != division_id,
                    )
                )
                if existing:
                    raise ValueError("division code already exists")
                row.code = code
            if name is not None and name != row.name:
                same_name = db.scalar(
                    select(ContestDivisionRow).where(
                        ContestDivisionRow.contest_id == contest_id,
                        ContestDivisionRow.name == name,
                        ContestDivisionRow.division_id != division_id,
                    )
                )
                if same_name:
                    raise ValueError("division name already exists")
                row.name = name
            if description is not None:
                row.description = description
            if display_order is not None:
                row.display_order = display_order
            db.commit()
            db.refresh(row)
            return _division(row)

    def delete_contest_division(self, contest_id: str, division_id: str) -> bool:
        with self._session() as db:
            row = db.scalar(select(ContestDivisionRow).where(
                ContestDivisionRow.division_id == division_id,
                ContestDivisionRow.contest_id == contest_id,
            ).with_for_update())
            if not row:
                return False
            dependencies = {}
            for model, label in ((ParticipantTeamRow, "참가팀"), (ProblemRow, "문제"),
                                 (SubmissionRow, "제출"), (JudgeJobRow, "채점 작업"),
                                 (TeamSessionRow, "참가자 세션"), (ScoreboardReleaseRow, "순위 발표 기록")):
                count = int(db.scalar(select(func.count()).select_from(model).where(model.division_id == division_id)) or 0)
                if count:
                    dependencies[label] = count
            if dependencies:
                summary = ", ".join(f"{label} {count}개" for label, count in dependencies.items())
                raise AppError(409, "division_in_use", f"연결된 {summary}가 있어 참가 유형을 삭제할 수 없습니다. 참가팀과 문제를 먼저 정리해 주세요.", dependencies)
            db.delete(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise AppError(409, "division_in_use", "연결된 기록이 있어 참가 유형을 삭제할 수 없습니다. 목록을 새로고침해 주세요.") from None
            return True

    def upsert_contest_operator(
        self, contest_id: str, email: str, display_name: str,
        roles: list[str] | None = None, *, protected_master: bool = False, actor: StaffAccount | None = None,
    ) -> StaffAccount:
        # Default is retained for internal/bootstrap callers; HTTP operator requests
        # always require an explicit validated role selection.
        # The legacy protected_master flag now requests full access; only the
        # first assignment becomes the owner, later assignments remain masters.
        roles = ["master"] if protected_master or roles is None else roles
        if "owner" in roles:
            raise AppError(409, "contest_owner_immutable", "대회 총괄은 권한 위임으로만 변경할 수 있습니다.")
        permissions = permissions_for_roles(roles)
        if not display_name.strip():
            raise ValueError("display name is required")
        with self._session() as db:
            contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            if not contest:
                raise ValueError("contest not found")
            if not contest.owner_staff_account_id and roles != ["master"]:
                raise AppError(409, "contest_owner_required", "첫 운영자는 대회 마스터로 배정해 주세요. 최초 배정 시 대회 총괄이 됩니다.")
            normalized_email = email.strip().lower()
            participant_conflicts = self._contest_participant_email_conflicts(db, contest_id, [normalized_email])
            if participant_conflicts:
                raise ValueError(f"operator email cannot be participant email: {participant_conflicts[0]}")
            account = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == normalized_email).with_for_update())
            if account and account.is_service_master:
                raise ValueError(SERVICE_MASTER_OPERATOR_ERROR)
            if not account:
                account = StaffAccountRow(email=normalized_email, display_name=display_name.strip(), is_service_master=False, permissions="", contest_scopes="{}", contest_roles="{}", protected_master_contests="[]")
                db.add(account)
            scopes = json.loads(account.contest_scopes or "{}")
            selections = json.loads(account.contest_roles or "{}")
            protected = json.loads(account.protected_master_contests or "[]")
            self._check_operator_mutation(db, contest, account, roles, actor)
            if not contest.owner_staff_account_id:
                db.flush()
                claimed = db.execute(update(ContestRow).where(ContestRow.contest_id == contest_id, ContestRow.owner_staff_account_id.is_(None)).values(owner_staff_account_id=account.staff_account_id))
                if claimed.rowcount != 1:
                    raise AppError(409, "contest_owner_changed", "총괄이 이미 배정되었습니다. 운영자 목록을 새로 확인해 주세요.")
            if contest.owner_staff_account_id == account.staff_account_id:
                roles = ["owner"]
                permissions = permissions_for_roles(roles)
            if selections.get(contest_id) == ["participant_preview"] and roles != ["participant_preview"]:
                db.execute(delete(ParticipantPreviewSessionRow).where(ParticipantPreviewSessionRow.staff_account_id == account.staff_account_id, ParticipantPreviewSessionRow.contest_id == contest_id))
            scopes[contest_id] = permissions
            selections[contest_id] = roles
            if contest.owner_staff_account_id == account.staff_account_id and contest_id not in protected:
                protected.append(contest_id)
            account.display_name = display_name.strip()
            account.contest_scopes = json.dumps(scopes)
            account.contest_roles = json.dumps(selections)
            account.protected_master_contests = json.dumps(protected)
            db.commit()
            db.refresh(account)
            return _staff(account)

    def update_contest_operator(
        self, contest_id: str, email: str, display_name: str, roles: list[str],
        *, new_email: str | None = None, actor: StaffAccount | None = None,
    ) -> StaffAccount | None:
        from app.services.errors import AppError
        from app.services.staff_identity import rename_staff_identity

        normalized_email = email.strip().lower()
        if "owner" in roles:
            raise AppError(409, "contest_owner_immutable", "대회 총괄은 권한 위임으로만 변경할 수 있습니다.")
        permissions = permissions_for_roles(roles)
        if not display_name.strip():
            raise ValueError("display name is required")
        try:
            with self._session() as db:
                contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
                if not contest:
                    return None
                account = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == normalized_email).with_for_update())
                if not account or account.is_service_master:
                    return None
                scopes = json.loads(account.contest_scopes or "{}")
                if not scopes.get(contest_id):
                    return None
                selections = json.loads(account.contest_roles or "{}")
                self._check_operator_mutation(db, contest, account, roles, actor)
                if contest.owner_staff_account_id == account.staff_account_id:
                    roles = ["owner"]
                    permissions = permissions_for_roles(roles)
                if new_email:
                    rename_staff_identity(db, account, actor.staff_account_id if actor else None, new_email)
                participant_conflicts = self._contest_participant_email_conflicts(db, contest_id, [account.email])
                if participant_conflicts:
                    raise AppError(409, "email_change_participant_identity", "이 이메일은 같은 대회의 참가자 계정에도 연결되어 있어 운영자 정보를 변경할 수 없습니다.")
                if selections.get(contest_id) == ["participant_preview"] and roles != ["participant_preview"]:
                    db.execute(delete(ParticipantPreviewSessionRow).where(ParticipantPreviewSessionRow.staff_account_id == account.staff_account_id, ParticipantPreviewSessionRow.contest_id == contest_id))
                scopes[contest_id] = permissions
                selections[contest_id] = roles
                account.display_name = display_name.strip()
                account.contest_scopes = json.dumps(scopes)
                account.contest_roles = json.dumps(selections)
                db.commit()
                db.refresh(account)
                return _staff(account)
        except IntegrityError as error:
            if new_email and new_email.strip().lower() != normalized_email:
                raise AppError(409, "email_already_in_use", "이미 다른 계정에서 사용한 이메일입니다. 다른 이메일을 입력해 주세요.") from error
            raise

    def remove_contest_operator(self, contest_id: str, email: str, *, actor: StaffAccount | None = None) -> StaffAccount | None:
        with self._session() as db:
            contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            if not contest:
                return None
            normalized_email = email.strip().lower()
            account = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == normalized_email).with_for_update())
            if not account or account.is_service_master:
                return None
            scopes = json.loads(account.contest_scopes or "{}")
            if not scopes.get(contest_id):
                return None
            self._check_operator_mutation(db, contest, account, None, actor)
            db.execute(delete(ParticipantPreviewSessionRow).where(ParticipantPreviewSessionRow.staff_account_id == account.staff_account_id, ParticipantPreviewSessionRow.contest_id == contest_id))
            scopes.pop(contest_id, None)
            selections = json.loads(account.contest_roles or "{}")
            selections.pop(contest_id, None)
            account.contest_scopes = json.dumps(scopes)
            account.contest_roles = json.dumps(selections)
            db.commit()
            db.refresh(account)
            return _staff(account)

    def _check_operator_mutation(self, db, contest, target, roles, actor) -> None:
        # Called after locking the contest: a concurrent transfer cannot turn the
        # target into an owner between the permission check and the mutation.
        cid = contest.contest_id
        if actor:
            current_actor = db.get(StaffAccountRow, actor.staff_account_id)
            scopes = json.loads(current_actor.contest_scopes or "{}").get(cid, []) if current_actor else []
            target_master = "contest.*" in json.loads(target.contest_scopes or "{}").get(cid, [])
            if not current_actor or (not current_actor.is_service_master and (
                "contest.*" not in scopes and (
                    "contest.staff.manage" not in scopes or target_master or roles == ["master"]
                )
            )):
                raise permission_denied()
        if contest.owner_staff_account_id == target.staff_account_id and roles != ["master"]:
            raise AppError(409, "contest_owner_immutable", "대회 총괄은 강등하거나 제거할 수 없습니다. 먼저 다른 운영자에게 총괄을 위임해 주세요.")

    def transfer_contest_owner(self, contest_id: str, target_email: str, actor: StaffAccount) -> list[StaffAccount]:
        with self._session() as db:
            contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            if not contest:
                raise AppError(404, "not_found", "대회를 찾을 수 없습니다.")
            # The authoritative owner id is checked again under the same lock as
            # the update; stale tokens and concurrent requests cannot take over.
            if contest.owner_staff_account_id != actor.staff_account_id:
                raise AppError(403, "contest_owner_transfer_denied", "현재 대회 총괄만 총괄 권한을 위임할 수 있습니다.")
            accounts = db.scalars(select(StaffAccountRow).where(
                (StaffAccountRow.staff_account_id == actor.staff_account_id) |
                (func.lower(StaffAccountRow.email) == target_email.strip().lower())
            ).order_by(StaffAccountRow.staff_account_id).with_for_update()).all()
            previous = next((a for a in accounts if a.staff_account_id == actor.staff_account_id), None)
            target = next((a for a in accounts if a.email.lower() == target_email.strip().lower()), None)
            if not target or target.is_service_master or not json.loads(target.contest_scopes or "{}").get(contest_id):
                raise AppError(422, "contest_owner_target_invalid", "이 대회에 등록된 운영자를 선택해 주세요.")
            if target is previous:
                raise AppError(422, "contest_owner_target_invalid", "다른 운영자에게 위임해 주세요.")
            if self._contest_participant_email_conflicts(db, contest_id, [target.email]):
                raise AppError(409, "contest_owner_target_invalid", "같은 대회의 참가자 계정에는 위임할 수 없습니다.")
            for account, roles in [(previous, ["master"]), (target, ["owner"])]:
                scopes = json.loads(account.contest_scopes or "{}")
                selections = json.loads(account.contest_roles or "{}")
                protected = [cid for cid in json.loads(account.protected_master_contests or "[]") if cid != contest_id]
                scopes[contest_id] = permissions_for_roles(roles)
                selections[contest_id] = roles
                if account is target:
                    protected.append(contest_id)
                account.contest_scopes = json.dumps(scopes)
                account.contest_roles = json.dumps(selections)
                account.protected_master_contests = json.dumps(protected)
            db.execute(delete(ParticipantPreviewSessionRow).where(ParticipantPreviewSessionRow.staff_account_id == target.staff_account_id, ParticipantPreviewSessionRow.contest_id == contest_id))
            claimed = db.execute(update(ContestRow).where(ContestRow.contest_id == contest_id, ContestRow.owner_staff_account_id == actor.staff_account_id).values(owner_staff_account_id=target.staff_account_id))
            if claimed.rowcount != 1:
                raise AppError(409, "contest_owner_changed", "총괄이 이미 변경되었습니다. 운영자 목록을 새로 확인해 주세요.")
            db.commit()
            return [_staff(previous), _staff(target)]

    def contest_operator_accounts(self, contest_id: str) -> list[StaffAccount]:
        with self._session() as db:
            rows = db.scalars(select(StaffAccountRow).where(StaffAccountRow.is_service_master.is_(False)).order_by(StaffAccountRow.email)).all()
            return [_staff(row) for row in rows if json.loads(row.contest_scopes or "{}").get(contest_id)]

    def is_service_master_email(self, email: str) -> bool:
        normalized_email = email.strip().lower()
        if not normalized_email:
            return False
        with self._session() as db:
            account = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == normalized_email))
            return bool(account and account.is_service_master)

    def accessible_contests_for_staff(self, account: StaffAccount) -> list[Contest]:
        if account.is_service_master:
            return sorted(self.contests.values(), key=lambda item: item.start_at)
        contest_ids = {contest_id for contest_id, scopes in account.contest_scopes.items() if any(scope != "contest.participant.preview" for scope in scopes)}
        contests = [contest for contest_id, contest in self.contests.items() if contest_id in contest_ids]
        return sorted(contests, key=lambda item: item.start_at)

    def notify_contest_operators(
        self,
        contest_id: str,
        mail_type: str,
        subject: str,
        body_text: str,
        exclude_emails: set[str] | None = None,
    ) -> list[MailQueueItem]:
        queued: list[MailQueueItem] = []
        excluded = exclude_emails or set()
        for account in self.contest_operator_accounts(contest_id):
            if account.email in excluded:
                continue
            queued.append(self.enqueue_mail(mail_type, str(account.email), subject, body_text, contest_id=contest_id))
        return queued

    def _contest_accepts_participant_invites(self, status: str) -> bool:
        return status in {ContestStatus.OPEN.value, ContestStatus.RUNNING.value}

    def enqueue_participant_invites_for_contest(self, contest_id: str) -> int:
        with self._session() as db:
            contest = db.get(ContestRow, contest_id)
            if not contest or not self._contest_accepts_participant_invites(contest.status):
                return 0
            teams = db.scalars(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.contest_id == contest_id)
            ).all()
            queued_count = 0
            for team in teams:
                division = db.get(ContestDivisionRow, team.division_id)
                if not division:
                    continue
                queued_count += self._enqueue_participant_invite_rows(db, contest, division, team)
            db.commit()
            return queued_count

    def enqueue_participant_invite_for_team(self, contest_id: str, participant_team_id: str) -> int:
        with self._session() as db:
            contest = db.get(ContestRow, contest_id)
            if not contest or not self._contest_accepts_participant_invites(contest.status):
                return 0
            team = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(
                    ParticipantTeamRow.contest_id == contest_id,
                    ParticipantTeamRow.participant_team_id == participant_team_id,
                )
            )
            if not team:
                return 0
            division = db.get(ContestDivisionRow, team.division_id)
            if not division:
                return 0
            queued_count = self._enqueue_participant_invite_rows(db, contest, division, team)
            db.commit()
            return queued_count

    def _enqueue_participant_invite_rows(
        self,
        db: Session,
        contest: ContestRow,
        division: ContestDivisionRow,
        team: ParticipantTeamRow,
    ) -> int:
        if team.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX):
            return 0
        content = participant_invite_mail(
            contest_title=contest.title,
            organization_name=contest.organization_name,
            team_name=team.team_name,
            division_name=division.name,
            contest_url=absolute_url(f"/contests/{contest.contest_id}"),
        )
        queued_count = 0
        for member in team.members:
            if is_internal_mail_recipient(member.email):
                continue
            if self._mail_exists(db, "participant_invited", member.email, content.subject, contest.contest_id):
                continue
            db.add(
                MailQueueItemRow(
                    mail_type="participant_invited",
                    contest_id=contest.contest_id,
                    recipient_email=member.email,
                    subject=content.subject,
                    body_text=content.body_text,
                    body_html=content.body_html,
                )
            )
            queued_count += 1
        return queued_count

    def contest_notices_for_view(
        self,
        contest_id: str,
        participant: dict | None = None,
        operator: bool = False,
        include_participant_visible: bool = False,
    ) -> list[ContestNotice]:
        with self._session() as db:
            rows = db.scalars(
                select(ContestNoticeRow).where(ContestNoticeRow.contest_id == contest_id).order_by(ContestNoticeRow.pinned.desc(), ContestNoticeRow.published_at.desc())
            ).all()
            if operator or participant or include_participant_visible:
                visible = rows
            else:
                visible = [row for row in rows if row.visibility == "public"]
            return [_contest_notice(row) for row in visible]

    def create_contest_notice(
        self,
        contest_id: str,
        title: str,
        body: str,
        pinned: bool = False,
        emergency: bool = False,
        visibility: str = "public",
        created_by_email: str | None = None,
        notice_id: str | None = None,
    ) -> ContestNotice:
        with self._session() as db:
            contest = db.get(ContestRow, contest_id)
            if not contest:
                raise ValueError("contest not found")
            row = ContestNoticeRow(
                contest_id=contest_id,
                title=title,
                body=body,
                pinned=pinned,
                emergency=emergency,
                visibility=visibility,
                created_by_email=created_by_email,
            )
            if notice_id is not None:
                row.contest_notice_id = notice_id
            db.add(row)
            if emergency:
                contest.emergency_notice = body
            from app.services.notifications import create_notice
            create_notice(db, row)
            # Contest notices are visible in-app. Disable notice email
            # fan-out to keep transactional mail volume focused on OTP
            # and direct question/answer flows during live operations.
            # content = contest_notice_mail(
            #     contest_title=contest.title,
            #     organization_name=contest.organization_name,
            #     notice_title=title,
            #     notice_body=body,
            #     notice_url=absolute_url(f"/contests/{contest_id}/board"),
            #     pinned=pinned,
            #     emergency=emergency,
            # )
            # participant_emails = db.scalars(
            #     select(TeamMemberRow.email)
            #     .join(
            #         ParticipantTeamRow,
            #         TeamMemberRow.participant_team_id == ParticipantTeamRow.participant_team_id,
            #     )
            #     .where(
            #         TeamMemberRow.contest_id == contest_id,
            #         ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
            #     )
            #     .order_by(TeamMemberRow.email)
            # ).all()
            # notified: set[str] = set()
            # for email in participant_emails:
            #     normalized = email.strip().lower()
            #     if is_internal_mail_recipient(normalized) or normalized in notified:
            #         continue
            #     notified.add(normalized)
            #     db.add(
            #         MailQueueItemRow(
            #             mail_type="contest_notice_created",
            #             recipient_email=normalized,
            #             subject=content.subject,
            #             body_text=content.body_text,
            #             body_html=content.body_html,
            #         )
            #     )
            db.commit()
            db.refresh(row)
            return _contest_notice(row)

    def update_contest_notice(self, contest_id: str, notice_id: str, **values) -> ContestNotice | None:
        allowed = {"title", "body", "pinned", "emergency", "visibility"}
        with self._session() as db:
            row = db.get(ContestNoticeRow, notice_id)
            if not row or row.contest_id != contest_id:
                return None
            for key, value in values.items():
                if key in allowed and value is not None:
                    setattr(row, key, value)
            contest = db.get(ContestRow, contest_id)
            if contest and row.emergency:
                contest.emergency_notice = row.body
            row.updated_at = now_utc()
            db.commit()
            db.refresh(row)
            return _contest_notice(row)

    def delete_contest_notice(self, contest_id: str, notice_id: str) -> bool:
        with self._session() as db:
            row = db.get(ContestNoticeRow, notice_id)
            if not row or row.contest_id != contest_id:
                return False
            db.delete(row)
            db.commit()
            return True

    def questions_for_view(self, contest_id: str, participant: dict | None = None, operator: bool = False) -> list[ContestQuestion]:
        participant_team_id = participant["team"].participant_team_id if participant else None
        with self._session() as db:
            rows = db.scalars(
                select(ContestQuestionRow)
                .where(ContestQuestionRow.contest_id == contest_id)
                .options(selectinload(ContestQuestionRow.answers))
                .order_by(ContestQuestionRow.created_at.desc())
            ).all()
            visible_questions = []
            for row in rows:
                if not operator and row.visibility == "private" and row.participant_team_id != participant_team_id:
                    continue
                visible_answers = []
                for answer in row.answers:
                    if operator or answer.visibility == "public" or row.participant_team_id == participant_team_id:
                        visible_answers.append(answer)
                visible_questions.append(
                    _question_for_view(row, db, visible_answers)
                )
            return visible_questions

    def create_question(self, contest_id: str, participant: dict, title: str, body: str, visibility: str) -> ContestQuestion:
        with self._session() as db:
            if not db.get(ContestRow, contest_id):
                raise ValueError("contest not found")
            row = ContestQuestionRow(
                contest_id=contest_id,
                participant_team_id=participant["team"].participant_team_id,
                team_member_id=participant["member"].team_member_id,
                title=title,
                body=body,
                visibility=visibility,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _question_for_view(row, db)

    def update_question(self, contest_id: str, question_id: str, **values) -> ContestQuestion | None:
        allowed = {"visibility"}
        with self._session() as db:
            row = db.get(ContestQuestionRow, question_id)
            if not row or row.contest_id != contest_id:
                return None
            for key, value in values.items():
                if key in allowed and value is not None:
                    setattr(row, key, value)
            row.updated_at = now_utc()
            db.commit()
            db.refresh(row)
            return _question_for_view(row, db)

    def delete_question(self, contest_id: str, question_id: str) -> bool:
        with self._session() as db:
            row = db.get(ContestQuestionRow, question_id)
            if not row or row.contest_id != contest_id:
                return False
            db.delete(row)
            db.commit()
            return True

    def create_answer(self, contest_id: str, question_id: str, body: str, visibility: str, created_by_email: str | None = None) -> ContestQuestionAnswer | None:
        with self._session() as db:
            question = db.get(ContestQuestionRow, question_id)
            if not question or question.contest_id != contest_id:
                return None
            row = ContestQuestionAnswerRow(
                contest_id=contest_id,
                contest_question_id=question_id,
                body=body,
                visibility=visibility,
                created_by_email=created_by_email,
            )
            db.add(row)
            from app.services.notifications import create_answer
            create_answer(db, row, question)
            db.commit()
            db.refresh(row)
            return _answer_for_view(row, db)

    def update_answer(self, contest_id: str, question_id: str, answer_id: str, **values) -> ContestQuestionAnswer | None:
        allowed = {"visibility"}
        with self._session() as db:
            row = db.get(ContestQuestionAnswerRow, answer_id)
            if not row or row.contest_id != contest_id or row.contest_question_id != question_id:
                return None
            for key, value in values.items():
                if key in allowed and value is not None:
                    setattr(row, key, value)
            row.updated_at = now_utc()
            db.commit()
            db.refresh(row)
            return _answer_for_view(row, db)

    def delete_answer(self, contest_id: str, question_id: str, answer_id: str) -> bool:
        with self._session() as db:
            row = db.get(ContestQuestionAnswerRow, answer_id)
            if not row or row.contest_id != contest_id or row.contest_question_id != question_id:
                return False
            db.delete(row)
            db.commit()
            return True

    def get_contest_question(self, contest_id: str, question_id: str) -> ContestQuestion | None:
        with self._session() as db:
            row = db.scalar(
                select(ContestQuestionRow)
                .options(selectinload(ContestQuestionRow.answers))
                .where(
                    ContestQuestionRow.contest_question_id == question_id,
                    ContestQuestionRow.contest_id == contest_id,
                )
            )
            if not row:
                return None
            return _question_for_view(row, db)

    def participant_team_member_emails(self, contest_id: str, participant_team_id: str) -> list[str]:
        with self._session() as db:
            rows = db.scalars(
                select(TeamMemberRow.email)
                .where(
                    TeamMemberRow.contest_id == contest_id,
                    TeamMemberRow.participant_team_id == participant_team_id,
                )
                .order_by(TeamMemberRow.role.desc(), TeamMemberRow.name.asc())
            ).all()
            emails = []
            seen = set()
            for email in rows:
                normalized = email.strip().lower()
                if is_internal_mail_recipient(normalized) or normalized in seen:
                    continue
                seen.add(normalized)
                emails.append(normalized)
            return emails

    def create_service_notice(self, title: str, summary: str, body: str, emergency: bool = False) -> ServiceNotice:
        with self._session() as db:
            row = ServiceNoticeRow(title=title, summary=summary, body=body, emergency=emergency)
            db.add(row)
            db.commit()
            db.refresh(row)
            return _notice(row)

    def update_service_notice(self, notice_id: str, **values) -> ServiceNotice | None:
        allowed = {"title", "summary", "body", "emergency"}
        with self._session() as db:
            row = db.get(ServiceNoticeRow, notice_id)
            if not row:
                return None
            for key, value in values.items():
                if key in allowed and value is not None:
                    setattr(row, key, value)
            db.commit()
            db.refresh(row)
            return _notice(row)

    def delete_service_notice(self, notice_id: str) -> bool:
        with self._session() as db:
            row = db.get(ServiceNoticeRow, notice_id)
            if not row:
                return False
            db.delete(row)
            db.commit()
            return True

    def create_contact_inquiry(self, title: str, sender_name: str, sender_email: str, body: str) -> ContactInquiry:
        with self._session() as db:
            row = ContactInquiryRow(
                title=title,
                sender_name=sender_name,
                sender_email=sender_email,
                body=body,
                status="pending",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _contact_inquiry(row)

    def answer_contact_inquiry(self, inquiry_id: str, answer_body: str, answered_by_email: str) -> ContactInquiry | None:
        with self._session() as db:
            row = db.get(ContactInquiryRow, inquiry_id)
            if not row:
                return None
            now = now_utc()
            previous_answer = (row.answer_body or "").strip()
            answer_header = f"[{now.isoformat()}] {answered_by_email}"
            next_answer = f"{answer_header}\n{answer_body}"
            row.answer_body = (
                f"{previous_answer}\n\n---\n\n{next_answer}"
                if previous_answer
                else next_answer
            )
            row.answered_by_email = answered_by_email
            row.answered_at = now
            row.updated_at = now
            row.status = "answered"
            db.commit()
            db.refresh(row)
            return _contact_inquiry(row)

    def update_contest_settings(self, contest_id: str, **values) -> Contest | None:
        allowed = {
            "title",
            "organization_name",
            "overview",
            "status",
            "visibility",
            "visibility_after_end",
            "start_at",
            "end_at",
            "freeze_at",
            "problem_access_after_end",
            "scoreboard_access_after_end",
            "submission_access_after_end",
            "board_access_after_end",
            "board_write_after_end",
            "notice_access_after_end",
            "editorial_access_after_end",
            "scoreboard_freeze_mode",
            "scoreboard_release_mode",
            "mock_judging_enabled",
            "participant_progress_visible",
            "mock_judging_progress_visible",
            "emergency_notice",
        }
        with self._session() as db:
            row = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            if not row:
                return None
            next_release_mode = values.get("scoreboard_release_mode")
            if next_release_mode is not None and next_release_mode != (row.scoreboard_release_mode or "manual"):
                if db.scalar(select(ScoreboardReleaseRow.division_id).where(ScoreboardReleaseRow.contest_id == contest_id, ScoreboardReleaseRow.mode != "not_started").limit(1)):
                    raise ValueError("순위 공개를 시작한 후에는 공개 방식을 변경할 수 없습니다.")
            next_freeze_mode = values.get("scoreboard_freeze_mode")
            if next_freeze_mode is not None and next_freeze_mode != row.scoreboard_freeze_mode:
                row.scoreboard_frozen_at = now_utc() if next_freeze_mode == ScoreboardFreezeMode.FROZEN else None
            for key, value in values.items():
                if key in allowed and (
                    value is not None or key == "emergency_notice"
                ):
                    if isinstance(value, (ContestStatus, ContestVisibility, ContestResourceAccess, ScoreboardFreezeMode, ScoreboardReleaseMode)):
                        value = value.value
                    setattr(row, key, value)
            normalize_private_resources(row)
            if row.problem_access_after_end == ContestResourceAccess.PRIVATE.value:
                row.editorial_access_after_end = ContestResourceAccess.PRIVATE.value
                row.mock_judging_enabled = False
            row.status = _schedule_status(row.status, row.start_at, row.end_at, now_utc())
            db.commit()
            db.refresh(row)
            return _contest(row)

    def contest_divisions(self, contest_id: str) -> list[ContestDivision]:
        with self._session() as db:
            rows = db.scalars(
                select(ContestDivisionRow).where(ContestDivisionRow.contest_id == contest_id).order_by(ContestDivisionRow.name)
            ).all()
            return [_division(row) for row in rows]

    def get_division(self, contest_id: str, division_id: str) -> ContestDivision | None:
        with self._session() as db:
            row = db.get(ContestDivisionRow, division_id)
            if not row or row.contest_id != contest_id:
                return None
            return _division(row)

    def get_team_by_email(self, contest_id: str, team_member_email: str) -> ParticipantTeam | None:
        if is_internal_mail_recipient(team_member_email):
            return None
        with self._session() as db:
            member = db.scalar(
                select(TeamMemberRow).where(TeamMemberRow.contest_id == contest_id, TeamMemberRow.email == team_member_email)
            )
            if not member:
                return None
            team = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == member.participant_team_id)
            )
            return _team(team) if team else None

    def verify_otp(self, contest_id: str, email: str, otp_code: str, force_new_session: bool = False) -> tuple[ParticipantTeam, TeamMember, ContestDivision, str] | None:
        with self._session() as db:
            otp = db.get(OtpCodeRow, email)
            demo_bypass = settings.allow_empty_otp and otp_code == ""
            if not demo_bypass and (not otp or otp.contest_id != contest_id or otp.code != otp_code or _aware(otp.expires_at) <= now_utc()):
                return None
            if otp:
                otp.verified_at = now_utc()
            member = db.scalar(select(TeamMemberRow).where(TeamMemberRow.contest_id == contest_id, TeamMemberRow.email == email))
            if not member:
                return None
            conflict = self._active_login_session_summary(db, email)
            if conflict and not force_new_session:
                raise SessionConflictError(conflict)
            if conflict and force_new_session:
                self._revoke_active_login_sessions_for_email(db, email)
            member.active_sessions += 1
            member.last_login_at = now_utc()
            team = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == member.participant_team_id)
            )
            if not team:
                return None
            team.status = "active"
            division = db.get(ContestDivisionRow, team.division_id)
            access_token = new_session_token(
                "participant_access",
                member.team_member_id,
                settings.participant_access_token_ttl_seconds,
                {"contest_id": contest_id, "team_id": team.participant_team_id, "division_id": team.division_id},
            )
            db.add(
                TeamSessionRow(
                    contest_id=contest_id,
                    division_id=team.division_id,
                    participant_team_id=team.participant_team_id,
                    team_member_id=member.team_member_id,
                    access_token_hash=token_hash(access_token),
                    issued_at=now_utc(),
                    expires_at=now_utc() + timedelta(seconds=settings.participant_access_token_ttl_seconds),
                )
            )
            db.commit()
            db.refresh(member)
            db.refresh(team)
            team_model = _team(team)
            member_model = team_model.members[[item.team_member_id for item in team.members].index(member.team_member_id)]
            return team_model, member_model, _division(division), access_token

    def create_participant_team(
        self,
        contest_id: str,
        division_id: str,
        team_name: str,
        leader_name: str,
        leader_email: str,
        members: list[tuple[str, str]],
    ) -> ParticipantTeam:
        with self._session() as db:
            emails = [leader_email.strip(), *[email.strip() for _, email in members]]
            normalized = [email.lower() for email in emails]
            duplicated_within_payload = sorted({email for email in normalized if normalized.count(email) > 1})
            if duplicated_within_payload:
                raise ValueError(f"participant email already registered: {', '.join(duplicated_within_payload)}")
            existing_emails = db.scalars(
                select(TeamMemberRow.email).where(
                    TeamMemberRow.contest_id == contest_id,
                    func.lower(TeamMemberRow.email).in_(normalized),
                )
            ).all()
            if existing_emails:
                conflicts = sorted({email.lower() for email in existing_emails})
                raise ValueError(f"participant email already registered: {', '.join(conflicts)}")
            staff_conflicts = self._contest_staff_email_conflicts(db, contest_id, normalized)
            if staff_conflicts:
                raise ValueError(f"participant email cannot be operator/staff account: {', '.join(staff_conflicts)}")
            team = ParticipantTeamRow(contest_id=contest_id, division_id=division_id, team_name=team_name, status="invited")
            db.add(team)
            db.flush()
            db.add(
                TeamMemberRow(
                    contest_id=contest_id,
                    participant_team_id=team.participant_team_id,
                    role=TeamMemberRole.LEADER.value,
                    name=leader_name,
                    email=leader_email,
                )
            )
            for name, email in members:
                db.add(
                    TeamMemberRow(
                        contest_id=contest_id,
                        participant_team_id=team.participant_team_id,
                        role=TeamMemberRole.MEMBER.value,
                        name=name,
                        email=email,
                    )
                )
            db.commit()
            row = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == team.participant_team_id)
            )
            return _team(row)

    def update_participant_team(
        self,
        contest_id: str,
        participant_team_id: str,
        team_name: str | None = None,
        division_id: str | None = None,
        status: str | None = None,
    ) -> ParticipantTeam | None:
        with self._session() as db:
            row = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == participant_team_id, ParticipantTeamRow.contest_id == contest_id)
            )
            if not row:
                return None
            if division_id is not None:
                division = db.get(ContestDivisionRow, division_id)
                if not division or division.contest_id != contest_id:
                    raise ValueError("division not found")
                row.division_id = division_id
            if team_name is not None:
                row.team_name = team_name
            if status is not None:
                row.status = status
            db.commit()
            db.refresh(row)
            return _team(row)

    def delete_participant_team(self, contest_id: str, participant_team_id: str) -> tuple[bool, str | None]:
        with self._session() as db:
            row = db.scalar(
                select(ParticipantTeamRow)
                .options(selectinload(ParticipantTeamRow.members))
                .where(ParticipantTeamRow.participant_team_id == participant_team_id, ParticipantTeamRow.contest_id == contest_id)
            )
            if not row:
                return False, "not_found"

            has_submission = db.scalar(
                select(SubmissionRow.submission_id)
                .where(SubmissionRow.contest_id == contest_id, SubmissionRow.participant_team_id == participant_team_id)
                .limit(1)
            )
            if has_submission:
                return False, "has_submission"

            has_question = db.scalar(
                select(ContestQuestionRow.contest_question_id)
                .where(ContestQuestionRow.contest_id == contest_id, ContestQuestionRow.participant_team_id == participant_team_id)
                .limit(1)
            )
            if has_question:
                return False, "has_question"

            db.query(TeamSessionRow).filter(
                TeamSessionRow.contest_id == contest_id,
                TeamSessionRow.participant_team_id == participant_team_id,
            ).delete()
            self._cancel_pending_participant_mail_rows(
                db,
                [member.email for member in row.members],
            )
            db.delete(row)
            db.commit()
            return True, None

    def add_team_member(
        self,
        contest_id: str,
        participant_team_id: str,
        name: str,
        email: str,
        role: TeamMemberRole = TeamMemberRole.MEMBER,
    ) -> TeamMember | None:
        with self._session() as db:
            team = db.get(ParticipantTeamRow, participant_team_id)
            if not team or team.contest_id != contest_id:
                return None
            if db.scalar(select(TeamMemberRow).where(TeamMemberRow.contest_id == contest_id, func.lower(TeamMemberRow.email) == email.lower())):
                raise ValueError(f"participant email already registered: {email.lower()}")
            staff_conflicts = self._contest_staff_email_conflicts(db, contest_id, [email.lower()])
            if staff_conflicts:
                raise ValueError(f"participant email cannot be operator/staff account: {staff_conflicts[0]}")
            row = TeamMemberRow(
                contest_id=contest_id,
                participant_team_id=participant_team_id,
                role=role.value,
                name=name,
                email=email,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return TeamMember(
                team_member_id=row.team_member_id,
                role=TeamMemberRole(row.role),
                name=row.name,
                email=row.email,
                active_sessions=row.active_sessions,
                last_login_at=_aware(row.last_login_at),
            )

    def update_team_member(
        self,
        contest_id: str,
        participant_team_id: str,
        team_member_id: str,
        name: str | None = None,
        email: str | None = None,
    ) -> TeamMember | None:
        with self._session() as db:
            row = db.scalar(
                select(TeamMemberRow).where(
                    TeamMemberRow.contest_id == contest_id,
                    TeamMemberRow.participant_team_id == participant_team_id,
                    TeamMemberRow.team_member_id == team_member_id,
                )
            )
            if not row:
                return None
            if email is not None and email != row.email:
                old_email = row.email
                exists = db.scalar(select(TeamMemberRow).where(TeamMemberRow.contest_id == contest_id, func.lower(TeamMemberRow.email) == email.lower()))
                if exists:
                    raise ValueError(f"participant email already registered: {email.lower()}")
                staff_conflicts = self._contest_staff_email_conflicts(db, contest_id, [email.lower()])
                if staff_conflicts:
                    raise ValueError(f"participant email cannot be operator/staff account: {staff_conflicts[0]}")
                row.email = email
                self._cancel_pending_participant_mail_rows(db, [old_email])
            if name is not None:
                row.name = name
            db.commit()
            db.refresh(row)
            return TeamMember(
                team_member_id=row.team_member_id,
                role=TeamMemberRole(row.role),
                name=row.name,
                email=row.email,
                active_sessions=row.active_sessions,
                last_login_at=_aware(row.last_login_at),
            )

    def revoke_team_member_sessions(self, contest_id: str, participant_team_id: str, team_member_id: str) -> TeamMember | None:
        with self._session() as db:
            member = db.scalar(
                select(TeamMemberRow).where(
                    TeamMemberRow.contest_id == contest_id,
                    TeamMemberRow.participant_team_id == participant_team_id,
                    TeamMemberRow.team_member_id == team_member_id,
                )
            )
            if not member:
                return None
            # A forced logout invalidates the account's login, refresh tokens,
            # and participant sessions across contests and devices together.
            self._revoke_active_login_sessions_for_email(db, member.email)
            db.commit()
            db.refresh(member)
            return TeamMember(
                team_member_id=member.team_member_id,
                role=TeamMemberRole(member.role),
                name=member.name,
                email=member.email,
                active_sessions=member.active_sessions,
                last_login_at=_aware(member.last_login_at),
            )

    def create_problem(
        self,
        contest_id: str,
        division_id: str,
        problem_code: str,
        title: str,
        statement: str,
        time_limit_ms: int,
        memory_limit_mb: int,
        language_resource_limits: dict | None,
        display_order: int,
        default_statement_language: str = "ko",
        statement_translations: dict | None = None,
        editorial_translations: dict | None = None,
        allowed_languages: list[str] | None = None,
    ) -> Problem:
        with self._session() as db:
            division = db.get(ContestDivisionRow, division_id)
            if not division or division.contest_id != contest_id:
                raise ValueError("division not found")
            row = ProblemRow(
                contest_id=contest_id,
                division_id=division_id,
                problem_code=problem_code,
                title=title,
                statement=statement,
                editorial="",
                **validate_statement_fields(default_statement_language, statement_translations or {}, editorial_translations or {}),
                time_limit_ms=time_limit_ms,
                memory_limit_mb=memory_limit_mb,
                language_resource_limits=language_resource_limits or {},
                allowed_languages=validate_languages(allowed_languages if allowed_languages is not None else list(LANGUAGES)),
                display_order=display_order,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _problem(row)

    def delete_problem(self, contest_id: str, problem_id: str) -> Problem | None:
        with self._session() as db:
            row = db.scalar(select(ProblemRow).where(ProblemRow.problem_id == problem_id).with_for_update())
            if not row or row.contest_id != contest_id:
                return None
            if rejudging.active_for_problem(db, problem_id):
                raise AppError(409, "rejudging_conflict", "이 문제의 재채점 작업을 먼저 반영하거나 취소해 주세요.")

            from app.services.verification_ai import delete_problem_reviews
            delete_problem_reviews(db, problem_id)
            rejudging.delete_problem_history(db, problem_id)
            item = _problem(row)
            deleted_storage_keys: set[str] = set()
            testcase_set_ids = list(
                db.scalars(
                    select(TestcaseSetRow.testcase_set_id).where(
                        TestcaseSetRow.problem_id == problem_id
                    )
                ).all()
            )
            if testcase_set_ids:
                cases = db.scalars(select(TestcaseRow).where(TestcaseRow.testcase_set_id.in_(testcase_set_ids))).all()
                for case in cases:
                    deleted_storage_keys.add(case.input_storage_key)
                    deleted_storage_keys.add(case.output_storage_key)
                db.execute(delete(TestcaseRow).where(TestcaseRow.testcase_set_id.in_(testcase_set_ids)))
                db.execute(delete(TestcaseSetRow).where(TestcaseSetRow.testcase_set_id.in_(testcase_set_ids)))

            assets = db.scalars(
                select(ProblemAssetRow).where(
                    ProblemAssetRow.contest_id == contest_id,
                    ProblemAssetRow.problem_id == problem_id,
                )
            ).all()
            for asset in assets:
                deleted_storage_keys.add(asset.storage_key)
            db.execute(
                delete(ProblemAssetRow).where(
                    ProblemAssetRow.contest_id == contest_id,
                    ProblemAssetRow.problem_id == problem_id,
                )
            )

            submission_ids = list(
                db.scalars(
                    select(SubmissionRow.submission_id).where(
                        SubmissionRow.contest_id == contest_id,
                        SubmissionRow.problem_id == problem_id,
                    )
                ).all()
            )
            if submission_ids:
                db.execute(delete(JudgeJobRow).where(JudgeJobRow.submission_id.in_(submission_ids)))
                db.execute(delete(SubmissionRow).where(SubmissionRow.submission_id.in_(submission_ids)))

            db.execute(
                delete(BundleWarmQueueItemRow).where(
                    BundleWarmQueueItemRow.contest_id == contest_id,
                    BundleWarmQueueItemRow.problem_id == problem_id,
                )
            )
            db.delete(row)
            db.commit()

            for key in deleted_storage_keys:
                try:
                    object_storage.delete(key)
                except Exception:
                    pass
            return item

    def copy_problem_to_division(
        self,
        contest_id: str,
        source_problem_id: str,
        target_division_id: str,
        problem_code: str | None = None,
        display_order: int | None = None,
    ) -> Problem:
        with self._session() as db:
            source = db.get(ProblemRow, source_problem_id)
            division = db.get(ContestDivisionRow, target_division_id)
            if not source or source.contest_id != contest_id:
                raise ValueError("source problem not found")
            if not division or division.contest_id != contest_id:
                raise ValueError("division not found")

            target_problem_id = new_id()
            next_display_order = display_order
            if next_display_order is None:
                next_display_order = (db.scalar(select(func.max(ProblemRow.display_order)).where(ProblemRow.contest_id == contest_id, ProblemRow.division_id == target_division_id)) or 0) + 1

            target = ProblemRow(
                problem_id=target_problem_id,
                contest_id=contest_id,
                division_id=target_division_id,
                problem_code=(problem_code or source.problem_code).strip(),
                title=source.title,
                statement=source.statement,
                editorial=source.editorial or "",
                default_statement_language=source.default_statement_language or "ko",
                statement_translations=deepcopy(source.statement_translations or {}),
                editorial_translations=deepcopy(source.editorial_translations or {}),
                time_limit_ms=source.time_limit_ms,
                memory_limit_mb=source.memory_limit_mb,
                language_resource_limits=source.language_resource_limits or {},
                allowed_languages=list(source.allowed_languages or LANGUAGES),
                display_order=next_display_order,
            )
            db.add(target)
            db.flush()

            def copied_storage_key(storage_key: str) -> str:
                source_marker = f"/problems/{source_problem_id}/"
                target_marker = f"/problems/{target_problem_id}/"
                if source_marker in storage_key:
                    return storage_key.replace(source_marker, target_marker, 1)
                filename = storage_key.rsplit("/", 1)[-1]
                return f"contests/{contest_id}/problems/{target_problem_id}/copied/{new_id()}-{filename}"

            copied_objects: dict[str, str] = {}

            def copy_object(storage_key: str, content_type: str = "application/octet-stream") -> str:
                if storage_key in copied_objects:
                    return copied_objects[storage_key]
                next_key = copied_storage_key(storage_key)
                object_storage.write_bytes(next_key, object_storage.read_bytes(storage_key), content_type)
                copied_objects[storage_key] = next_key
                return next_key

            source_assets = db.scalars(
                select(ProblemAssetRow)
                .where(ProblemAssetRow.contest_id == contest_id, ProblemAssetRow.problem_id == source_problem_id)
                .order_by(ProblemAssetRow.created_at, ProblemAssetRow.asset_id)
            ).all()
            copied_asset_ids = {}
            for asset in source_assets:
                copied_asset_ids[asset.asset_id] = new_id()
                db.add(
                    ProblemAssetRow(
                        asset_id=copied_asset_ids[asset.asset_id],
                        contest_id=contest_id,
                        problem_id=target_problem_id,
                        original_filename=asset.original_filename,
                        storage_key=copy_object(asset.storage_key, asset.mime_type),
                        mime_type=asset.mime_type,
                        file_size=asset.file_size,
                        sha256=asset.sha256,
                        source_language=asset.source_language,
                        asset_status=asset.asset_status,
                    )
                )

            source_sets = db.scalars(
                select(TestcaseSetRow)
                .where(TestcaseSetRow.problem_id == source_problem_id)
                .order_by(TestcaseSetRow.version, TestcaseSetRow.testcase_set_id)
            ).all()
            from app.services.problem_archive import rewrite_problem_documents
            rewritten = rewrite_problem_documents({"statement": target.statement, "editorial": target.editorial,
                "statement_translations": target.statement_translations, "editorial_translations": target.editorial_translations},
                copied_asset_ids, {a.storage_key: copied_asset_ids[a.asset_id] for a in source_assets})
            for key, value in rewritten.items():
                setattr(target, key, value)
            for source_set in source_sets:
                target_set = TestcaseSetRow(
                    problem_id=target_problem_id,
                    version=source_set.version,
                    is_active=source_set.is_active,
                )
                db.add(target_set)
                db.flush()
                source_cases = db.scalars(
                    select(TestcaseRow)
                    .where(TestcaseRow.testcase_set_id == source_set.testcase_set_id)
                    .order_by(TestcaseRow.display_order, TestcaseRow.testcase_id)
                ).all()
                for case in source_cases:
                    db.add(
                        TestcaseRow(
                            testcase_set_id=target_set.testcase_set_id,
                            display_order=case.display_order,
                            input_storage_key=copy_object(case.input_storage_key, "text/plain"),
                            output_storage_key=copy_object(case.output_storage_key, "text/plain"),
                            input_sha256=case.input_sha256,
                            output_sha256=case.output_sha256,
                            time_limit_ms_override=case.time_limit_ms_override,
                            memory_limit_mb_override=case.memory_limit_mb_override,
                        )
                    )

            try:
                db.commit()
            except IntegrityError as error:
                db.rollback()
                raise ValueError("problem code already exists in this division") from error
            db.refresh(target)
            return _problem(target)

    def update_problem(self, contest_id: str, problem_id: str, **values) -> Problem | None:
        with self._session() as db:
            row = db.get(ProblemRow, problem_id)
            if not row or row.contest_id != contest_id:
                return None
            if any(k in values for k in ("default_statement_language", "statement_translations", "editorial_translations")):
                values.update(validate_statement_fields(
                    values.get("default_statement_language") or row.default_statement_language or "ko",
                    values.get("statement_translations") if values.get("statement_translations") is not None else row.statement_translations or {},
                    values.get("editorial_translations") if values.get("editorial_translations") is not None else row.editorial_translations or {}))
            if "allowed_languages" in values:
                values["allowed_languages"] = validate_languages(values["allowed_languages"])
            for key, value in values.items():
                if value is not None:
                    if key == "division_id":
                        division = db.get(ContestDivisionRow, value)
                        if not division or division.contest_id != contest_id:
                            raise ValueError("division not found")
                    if key == "problem_code":
                        value = str(value).strip()
                        if not value:
                            raise ValueError("problem code is required")
                    setattr(row, key, value)
            try:
                db.commit()
            except IntegrityError as error:
                db.rollback()
                raise ValueError("problem code already exists in this division") from error
            db.refresh(row)
            return _problem(row)

    def create_problem_asset(
        self,
        contest_id: str,
        problem_id: str,
        original_filename: str,
        storage_key: str,
        mime_type: str,
        file_size: int,
        sha256: str,
    ) -> ProblemAsset:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            row = ProblemAssetRow(
                contest_id=contest_id,
                problem_id=problem_id,
                original_filename=original_filename,
                storage_key=storage_key,
                mime_type=mime_type,
                file_size=file_size,
                sha256=sha256,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _asset(row)

    def problem_assets_for_problem(self, contest_id: str, problem_id: str) -> list[ProblemAsset]:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            rows = db.scalars(select(ProblemAssetRow).where(ProblemAssetRow.contest_id == contest_id, ProblemAssetRow.problem_id == problem_id)).all()
            return [_asset(row) for row in rows]

    def get_problem_asset(self, contest_id: str, problem_id: str, asset_id: str) -> ProblemAsset | None:
        with self._session() as db:
            row = db.get(ProblemAssetRow, asset_id)
            if not row or row.contest_id != contest_id or row.problem_id != problem_id:
                return None
            return _asset(row)

    def delete_problem_asset(self, contest_id: str, problem_id: str, asset_id: str) -> ProblemAsset | None:
        with self._session() as db:
            row = db.get(ProblemAssetRow, asset_id)
            if not row or row.contest_id != contest_id or row.problem_id != problem_id:
                return None
            asset = _asset(row)
            db.delete(row)
            db.commit()
            try:
                object_storage.delete(asset.storage_key)
            except Exception:
                pass
            return asset

    def create_testcase_set(self, contest_id: str, problem_id: str, is_active: bool) -> TestcaseSet:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            version = (db.scalar(select(func.max(TestcaseSetRow.version)).where(TestcaseSetRow.problem_id == problem_id)) or 0) + 1
            if is_active:
                for existing in db.scalars(select(TestcaseSetRow).where(TestcaseSetRow.problem_id == problem_id)).all():
                    existing.is_active = False
            row = TestcaseSetRow(problem_id=problem_id, version=version, is_active=is_active)
            db.add(row)
            db.commit()
            db.refresh(row)
            return _testcase_set(row)

    def update_testcase_set(self, contest_id: str, problem_id: str, testcase_set_id: str, is_active: bool | None = None) -> TestcaseSet | None:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            row = db.get(TestcaseSetRow, testcase_set_id)
            if not problem or problem.contest_id != contest_id or not row or row.problem_id != problem_id:
                return None
            if is_active is not None:
                if is_active:
                    for existing in db.scalars(select(TestcaseSetRow).where(TestcaseSetRow.problem_id == problem_id)).all():
                        existing.is_active = existing.testcase_set_id == testcase_set_id
                else:
                    row.is_active = False
            db.commit()
            db.refresh(row)
            return _testcase_set(row)

    def add_testcase(
        self,
        contest_id: str,
        problem_id: str,
        testcase_set_id: str,
        display_order: int,
        input_storage_key: str,
        output_storage_key: str,
        input_sha256: str,
        output_sha256: str,
        time_limit_ms_override: int | None,
        memory_limit_mb_override: int | None,
    ) -> Testcase:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            testcase_set = db.get(TestcaseSetRow, testcase_set_id)
            if not problem or problem.contest_id != contest_id or not testcase_set or testcase_set.problem_id != problem_id:
                raise ValueError("testcase set not found")
            row = TestcaseRow(
                testcase_set_id=testcase_set_id,
                display_order=display_order,
                input_storage_key=input_storage_key,
                output_storage_key=output_storage_key,
                input_sha256=input_sha256,
                output_sha256=output_sha256,
                time_limit_ms_override=time_limit_ms_override,
                memory_limit_mb_override=memory_limit_mb_override,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _testcase(row)

    def replace_testcases_in_set(
        self,
        contest_id: str,
        problem_id: str,
        testcase_set_id: str,
        cases: list[dict],
    ) -> list[Testcase]:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            testcase_set = db.get(TestcaseSetRow, testcase_set_id)
            if not problem or problem.contest_id != contest_id or not testcase_set or testcase_set.problem_id != problem_id:
                raise ValueError("testcase set not found")
            old_cases = db.scalars(select(TestcaseRow).where(TestcaseRow.testcase_set_id == testcase_set_id)).all()
            old_keys = {(row.input_storage_key, row.output_storage_key) for row in old_cases}
            new_keys = {(item["input_storage_key"], item["output_storage_key"]) for item in cases}
            for row in old_cases:
                db.delete(row)
            db.flush()
            created_rows: list[TestcaseRow] = []
            for item in cases:
                row = TestcaseRow(
                    testcase_set_id=testcase_set_id,
                    display_order=item["display_order"],
                    input_storage_key=item["input_storage_key"],
                    output_storage_key=item["output_storage_key"],
                    input_sha256=item["input_sha256"],
                    output_sha256=item["output_sha256"],
                    time_limit_ms_override=item.get("time_limit_ms_override"),
                    memory_limit_mb_override=item.get("memory_limit_mb_override"),
                )
                db.add(row)
                created_rows.append(row)
            db.commit()
            for input_key, output_key in old_keys - new_keys:
                for key in (input_key, output_key):
                    try:
                        object_storage.delete(key)
                    except Exception:
                        pass
            return [_testcase(row) for row in created_rows]

    def delete_testcase(self, contest_id: str, problem_id: str, testcase_set_id: str, testcase_id: str) -> Testcase | None:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            testcase_set = db.get(TestcaseSetRow, testcase_set_id)
            row = db.get(TestcaseRow, testcase_id)
            if not problem or problem.contest_id != contest_id or not testcase_set or testcase_set.problem_id != problem_id or not row or row.testcase_set_id != testcase_set_id:
                return None
            item = _testcase(row)
            db.delete(row)
            db.commit()
            for key in (item.input_storage_key, item.output_storage_key):
                try:
                    object_storage.delete(key)
                except Exception:
                    pass
            return item

    def delete_testcase_set(self, contest_id: str, problem_id: str, testcase_set_id: str) -> TestcaseSet | None:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            row = db.get(TestcaseSetRow, testcase_set_id)
            if not problem or problem.contest_id != contest_id or not row or row.problem_id != problem_id:
                return None
            cases = db.scalars(select(TestcaseRow).where(TestcaseRow.testcase_set_id == testcase_set_id)).all()
            deleted_keys = []
            for case in cases:
                deleted_keys.append((case.input_storage_key, case.output_storage_key))
                db.delete(case)
            item = _testcase_set(row)
            was_active = bool(row.is_active)
            db.delete(row)
            db.flush()
            if was_active:
                fallback = db.scalar(
                    select(TestcaseSetRow).where(TestcaseSetRow.problem_id == problem_id).order_by(TestcaseSetRow.version.desc())
                )
                if fallback:
                    fallback.is_active = True
            db.commit()
            for input_key, output_key in deleted_keys:
                for key in (input_key, output_key):
                    try:
                        object_storage.delete(key)
                    except Exception:
                        pass
            return item

    def testcase_sets_for_problem(
        self,
        contest_id: str,
        problem_id: str,
        *,
        include_file_sizes: bool = False,
    ) -> list[dict]:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            sets = db.scalars(
                select(TestcaseSetRow)
                .where(TestcaseSetRow.problem_id == problem_id)
                .order_by(TestcaseSetRow.version)
            ).all()
            result = []
            for testcase_set in sets:
                cases = db.scalars(
                    select(TestcaseRow)
                    .where(TestcaseRow.testcase_set_id == testcase_set.testcase_set_id)
                    .order_by(TestcaseRow.display_order)
                ).all()
                if include_file_sizes:
                    testcases = [
                        _testcase(
                            case,
                            input_size_bytes=object_storage.size_bytes(
                                case.input_storage_key
                            ),
                            output_size_bytes=object_storage.size_bytes(
                                case.output_storage_key
                            ),
                        ).model_dump(mode="json")
                        for case in cases
                    ]
                else:
                    testcases = [_testcase(case).model_dump(mode="json") for case in cases]
                result.append(
                    {
                        **_testcase_set(testcase_set).model_dump(mode="json"),
                        "testcases": testcases,
                    }
                )
            return result

    def create_submission(self, contest_id: str, problem_id: str, team_member_email: str, language: str, source_code: str) -> Submission:
        source_code = _normalize_source_code(source_code)
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            require_allowed(problem, language)
            member = db.scalar(select(TeamMemberRow).where(TeamMemberRow.contest_id == contest_id, TeamMemberRow.email == team_member_email))
            if not member:
                raise ValueError("team member not registered")
            team = db.get(ParticipantTeamRow, member.participant_team_id)
            if not team or team.division_id != problem.division_id:
                raise ValueError("division mismatch")
            submission = SubmissionRow(
                contest_id=contest_id,
                division_id=team.division_id,
                problem_id=problem_id,
                participant_team_id=team.participant_team_id,
                team_member_id=member.team_member_id,
                language=language,
                source_code=source_code,
                status=SubmissionStatus.WAITING.value,
            )
            db.add(submission)
            db.flush()
            next_position = (db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0) + 1
            db.add(
                JudgeJobRow(
                    submission_id=submission.submission_id,
                    contest_id=contest_id,
                    division_id=team.division_id,
                    status=JudgeJobStatus.PENDING.value,
                    queue_position=next_position,
                )
            )
            db.commit()
            db.refresh(submission)
            return _submission(submission)

    def retry_system_error_submission(self, contest_id: str, submission_id: str):
        """Requeue the same submission once, keeping its source and original time."""
        with self._session() as db:
            # Match the job -> submission lock order used by result reporting.
            jobs = db.scalars(select(JudgeJobRow).where(
                JudgeJobRow.contest_id == contest_id,
                JudgeJobRow.submission_id == submission_id,
                ~rejudging.is_job(),
            ).order_by(JudgeJobRow.created_at.desc()).with_for_update()).all()
            submission = db.scalar(select(SubmissionRow).where(
                SubmissionRow.contest_id == contest_id,
                SubmissionRow.submission_id == submission_id,
            ).with_for_update())
            if not submission:
                raise AppError(404, "not_found", "제출을 찾을 수 없습니다.")
            if rejudging.active_for_submission(db, submission_id):
                raise AppError(409, "rejudging_conflict", "진행 중인 재채점 작업을 먼저 반영하거나 취소해 주세요.")
            from app.orm_models import VerificationTrialRow
            if db.get(VerificationTrialRow, submission_id):
                raise AppError(409, "verification_trial_immutable", "AI 실행 기록은 보존됩니다. 검증 요청창에서 다시 실행해 주세요.")
            if not jobs or submission.status != "system_error" or any(
                job.status in {"pending", "running"} for job in jobs
            ):
                raise AppError(409, "rejudge_not_available", "채점이 종료된 시스템 에러 제출만 재채점할 수 있습니다.")
            previous = {key: getattr(submission, key) for key in (
                "status", "compile_message", "judge_message", "failed_testcase_order",
                "runtime_ms", "memory_kb", "status_updated_at",
            )}
            changed = db.execute(update(SubmissionRow).where(
                SubmissionRow.submission_id == submission_id,
                SubmissionRow.status == "system_error",
            ).values(status="waiting", status_updated_at=now_utc(),
                compile_message=None, judge_message=None, failed_testcase_order=None,
                runtime_ms=None, memory_kb=None, progress_current=None, progress_total=None))
            if changed.rowcount != 1:
                raise AppError(409, "rejudge_not_available", "이미 재채점을 요청한 제출입니다.")
            job = jobs[0]
            job.status = "pending"
            job.assigned_node_id = None
            job.lease_token = None
            job.leased_at = None
            job.queue_position = (db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0) + 1
            from app.orm_models import VerificationRunRow
            run = db.get(VerificationRunRow, submission_id)
            if run:
                run.context_hash = None
                run.analysis_id = None
                from app.services.verification_execution import requeue
                requeue(db, submission_id)
            db.commit()
            db.refresh(submission)
            return _submission(submission), jsonable_encoder(previous)

    def create_operator_test_submission(
        self,
        contest_id: str,
        problem_id: str,
        language: str,
        source_code: str,
        *,
        verification_asset_id: str | None = None,
        submitted_by_name: str | None = None,
        submitted_by_email: str | None = None,
    ) -> Submission:
        source_code = _normalize_source_code(source_code)
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            submission = SubmissionRow(
                contest_id=contest_id,
                division_id=problem.division_id,
                problem_id=problem_id,
                participant_team_id=None,
                team_member_id=None,
                submission_kind="operator_test",
                submitted_by_name=submitted_by_name or "운영자",
                submitted_by_email=submitted_by_email,
                language=language,
                source_code=source_code,
                status=SubmissionStatus.WAITING.value,
            )
            db.add(submission)
            db.flush()
            if verification_asset_id:
                from app.services.verification_ai import attach_run
                attach_run(db, submission, verification_asset_id)
            next_position = (db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0) + 1
            db.add(
                JudgeJobRow(
                    submission_id=submission.submission_id,
                    contest_id=contest_id,
                    division_id=problem.division_id,
                    status=JudgeJobStatus.PENDING.value,
                    queue_position=next_position,
                )
            )
            db.commit()
            db.refresh(submission)
            return _submission(submission, staff_titles=_submission_staff_titles([submission], db))

    def create_mock_submission(self, contest_id: str, problem_id: str, language: str, source_code: str) -> Submission:
        source_code = _normalize_source_code(source_code)
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            require_allowed(problem, language)
            submission = SubmissionRow(
                contest_id=contest_id,
                division_id=problem.division_id,
                problem_id=problem_id,
                participant_team_id=None,
                team_member_id=None,
                submission_kind="mock_judging",
                submitted_by_name="모의채점",
                submitted_by_email=None,
                language=language,
                source_code=source_code,
                status=SubmissionStatus.WAITING.value,
            )
            db.add(submission)
            db.flush()
            next_position = (db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0) + 1
            db.add(
                JudgeJobRow(
                    submission_id=submission.submission_id,
                    contest_id=contest_id,
                    division_id=problem.division_id,
                    status=JudgeJobStatus.PENDING.value,
                    queue_position=next_position,
                )
            )
            db.commit()
            db.refresh(submission)
            return _submission(submission)

    @staticmethod
    def _scoreboard_ended(contest: ContestRow) -> bool:
        return contest.status in {"ended", "finalized", "archived"} or (
            contest.status not in {"draft", "schedule_tbd"} and now_utc() >= _aware(contest.end_at)
        )

    @staticmethod
    def _scoreboard_cutoff(contest: ContestRow, now: datetime, *, resolver: bool = False) -> datetime | None:
        freeze_at = _aware(contest.freeze_at)
        mode = contest.scoreboard_freeze_mode or "auto"
        if mode == "frozen":
            # Keep the exact click time, including when manually freezing before
            # the scheduled freeze. A later request must never move the cutoff.
            frozen_at = _aware(contest.scoreboard_frozen_at) or now
            return min(freeze_at, frozen_at)
        if resolver or (mode != "live" and freeze_at <= now):
            return freeze_at
        return None

    def scoreboard_release(self, contest_id: str, division_id: str) -> dict | None:
        with self._session() as db:
            contest = db.get(ContestRow, contest_id)
            division = db.get(ContestDivisionRow, division_id)
            if not contest or not division or division.contest_id != contest_id:
                return None
            release = db.get(ScoreboardReleaseRow, division_id)
            strategy = contest.scoreboard_release_mode or "manual"
            if strategy == "immediate" and self._scoreboard_ended(contest):
                team_count = db.scalar(select(func.count()).select_from(ParticipantTeamRow).where(
                    ParticipantTeamRow.contest_id == contest_id,
                    ParticipantTeamRow.division_id == division_id,
                    ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
                )) or 0
                result = (self._release_summary(release, include_undo=True)
                          if release and release.mode != "not_started"
                          else self._immediate_release_summary([], include_undo=True))
                result.update(total_count=team_count, revealed_count=team_count if result["mode"] == "all" else 0,
                              revision=(release.revision or 0) if release else 0)
                return result
            if release:
                return self._release_summary(release, strategy, include_undo=True)
            return self._release_summary(None, strategy, include_undo=True)

    @staticmethod
    def _immediate_release_summary(rows: list[dict], *, include_undo: bool = False) -> dict:
        result = {"mode": "all", "strategy": "immediate", "ranks": [],
                  "revealed_count": len(rows), "total_count": len(rows)}
        if include_undo:
            result.update(revision=0, undo={"action": "automatic"})
        return result

    @staticmethod
    def _release_summary(release: ScoreboardReleaseRow | None, strategy: str = "manual", *, include_undo: bool = False) -> dict:
        if release is None or release.mode == "not_started":
            result = {"mode": "not_started", "strategy": strategy, "ranks": [], "revealed_count": 0, "total_count": 0}
            if strategy == "resolver":
                result["resolver"] = {"step": 0, "total_steps": 0, "pending_count": 0, "last_event": None}
            if include_undo:
                result.update(revision=(release.revision or 0) if release else 0, undo=None)
            return result
        strategy = release.strategy or "manual"
        ranks = sorted({row["rank"] for row in release.snapshot_rows})
        revealed = set(release.revealed_ranks)
        result = {
            "mode": release.mode,
            "strategy": strategy,
            "ranks": [{"rank": rank, "team_count": sum(row["rank"] == rank for row in release.snapshot_rows), "revealed": rank in revealed} for rank in ranks],
            "revealed_count": sum(row["rank"] in revealed for row in release.snapshot_rows),
            "total_count": len(release.snapshot_rows),
        }
        if include_undo:
            undo = None
            if release.undo_history:
                entry = release.undo_history[-1]
                undo = {"action": entry["action"]}
                if entry.get("rank") is not None:
                    undo["rank"] = entry["rank"]
            elif release.undo_history is None:
                undo = {"action": "legacy"}
            result.update(revision=release.revision or 0, undo=undo)
        if strategy == "resolver":
            state = release.resolver_state or {}
            pending = state.get("pending", [])
            pending_teams = {event["team_id"] for event in pending}
            result["revealed_count"] = sum(row["team_id"] not in pending_teams for row in release.snapshot_rows)
            # Final ties and their team counts reveal future results too.
            if release.mode != "all":
                result["ranks"] = []
            result["resolver"] = {
                "step": state.get("step", 0), "total_steps": state.get("total_steps", 0),
                "pending_count": len(pending), "last_event": state.get("last_event"),
            }
        return result

    def _new_resolver_state(self, db: Session, contest: ContestRow, division_id: str) -> dict:
        cutoff = self._scoreboard_cutoff(contest, now_utc(), resolver=True)
        board = self.scoreboard_rows(contest.contest_id, division_id, public_view=False, _cutoff_at=cutoff)
        rows = jsonable_encoder(board["rows"])
        team_ids = {row["team_id"] for row in rows}
        scores = {(row["team_id"], score["problem_id"]): score for row in rows for score in row["problem_scores"]}
        solved = {key for key, score in scores.items() if score["solved"]}
        pending = []
        submissions = db.scalars(select(SubmissionRow).where(
            SubmissionRow.contest_id == contest.contest_id,
            SubmissionRow.division_id == division_id,
            SubmissionRow.submission_kind == "participant",
            SubmissionRow.submitted_at > cutoff,
        ).order_by(SubmissionRow.submitted_at, SubmissionRow.submission_id)).all()
        for submission in submissions:
            key = (submission.participant_team_id, submission.problem_id)
            if submission.participant_team_id not in team_ids or key not in scores or key in solved:
                continue
            submitted_at = _aware(submission.submitted_at)
            pending.append({
                "team_id": submission.participant_team_id, "problem_id": submission.problem_id,
                "problem_code": scores[key]["problem_code"], "status": submission.status,
                "submitted_at": submitted_at.isoformat(),
                "elapsed_minutes": max(0, int((submitted_at - _aware(contest.start_at)).total_seconds() // 60)),
            })
        return {"rows": rows, "pending": pending, "step": 0, "total_steps": len(pending), "last_event": None}

    @staticmethod
    def _rank_resolver_rows(rows: list[dict]) -> None:
        def rank_key(row: dict) -> tuple:
            return (-row["solved"], row["penalty"], sum(score["attempts"] for score in row["problem_scores"] if not score["solved"]))
        rows.sort(key=lambda row: (
            *rank_key(row),
            datetime.fromisoformat(row["last_solved_at"]) if row.get("last_solved_at") else datetime.max.replace(tzinfo=timezone.utc),
            row["team_name"],
        ))
        previous_key = None
        rank = 0
        for index, row in enumerate(rows, start=1):
            key = rank_key(row)
            if key != previous_key:
                rank = index
                previous_key = key
            row["rank"] = rank

    def _advance_resolver(self, release: ScoreboardReleaseRow) -> dict | None:
        state = deepcopy(release.resolver_state)
        pending = state["pending"]
        if not pending:
            return
        pending_teams = {event["team_id"] for event in pending}
        row = next(row for row in reversed(state["rows"]) if row["team_id"] in pending_teams)
        event_index = next(index for index, event in enumerate(pending) if event["team_id"] == row["team_id"])
        event = pending.pop(event_index)
        inverse = {"row": deepcopy(row), "event": event, "event_index": event_index,
                   "step": state["step"], "last_event": state.get("last_event")}
        old_rank = row["rank"]
        score = next(score for score in row["problem_scores"] if score["problem_id"] == event["problem_id"])
        if not score["solved"] and event["status"] == "accepted":
            score.update(solved=True, wrong_attempts=score["attempts"],
                         penalty=event["elapsed_minutes"] + score["attempts"] * 20,
                         solved_at=event["submitted_at"], best_status="accepted")
            row["solved"] += 1
            row["penalty"] += score["penalty"]
            if not row.get("last_solved_at") or datetime.fromisoformat(event["submitted_at"]) > datetime.fromisoformat(row["last_solved_at"]):
                row["last_solved_at"] = event["submitted_at"]
        elif not score["solved"] and event["status"] != "compile_error":
            score["attempts"] += 1
            score["wrong_attempts"] += 1
        row["submission_count"] += 1
        state["step"] += 1
        self._rank_resolver_rows(state["rows"])
        state["last_event"] = {
            "team_id": row["team_id"], "team_name": row["team_name"],
            "problem_code": event["problem_code"], "status": event["status"],
            "from_rank": old_rank, "to_rank": row["rank"],
        }
        release.resolver_state = state
        return inverse

    @staticmethod
    def _reset_scoreboard_release(release: ScoreboardReleaseRow) -> None:
        release.mode = "not_started"
        release.snapshot_rows = []
        release.revealed_ranks = []
        release.resolver_state = None
        release.undo_history = []

    def _undo_scoreboard_release(self, release: ScoreboardReleaseRow) -> None:
        if release.undo_history is None and release.mode != "not_started":
            # Earlier releases have no chronological action log. Do not invent
            # one from rank order or current (possibly rejudged) submissions.
            self._reset_scoreboard_release(release)
            return
        history = list(release.undo_history or [])
        if not history:
            raise ValueError("되돌릴 순위 공개 기록이 없습니다.")
        entry = history.pop()
        if entry["mode"] == "not_started":
            self._reset_scoreboard_release(release)
        else:
            release.mode = entry["mode"]
            release.revealed_ranks = entry["revealed_ranks"]
            if "resolver_state" in entry:
                release.resolver_state = entry["resolver_state"]
            elif "resolver_delta" in entry:
                inverse = entry["resolver_delta"]
                state = deepcopy(release.resolver_state)
                state["pending"].insert(inverse["event_index"], inverse["event"])
                state["rows"] = [inverse["row"] if row["team_id"] == inverse["row"]["team_id"] else row for row in state["rows"]]
                state.update(step=inverse["step"], last_event=inverse["last_event"])
                self._rank_resolver_rows(state["rows"])
                release.resolver_state = state
            release.undo_history = None if entry.get("legacy_base") and not history else history

    def update_scoreboard_release(self, contest_id: str, division_id: str, action: str, rank: int | None = None, expected_step: int | None = None, expected_revision: int | None = None) -> dict:
        with self._session() as db:
            # The contest lock serializes creation, undo, and forward actions.
            contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            division = db.get(ContestDivisionRow, division_id)
            if not contest or not division or division.contest_id != contest_id:
                raise ValueError("division not found")
            if not self._scoreboard_ended(contest):
                raise ValueError("대회 종료 후에 순위를 공개하거나 되돌릴 수 있습니다.")
            strategy = contest.scoreboard_release_mode or "manual"
            allowed = {"all", "undo"} if strategy == "immediate" else ({"start", "next", "all", "undo"} if strategy == "resolver" else {"start", "rank", "all", "undo"})
            if action not in allowed:
                raise ValueError("선택한 공개 방식에서 지원하지 않는 동작입니다.")
            release = db.get(ScoreboardReleaseRow, division_id)
            revision = (release.revision or 0) if release else 0
            if action == "undo" and expected_revision is None:
                raise ValueError("최신 공개 상태를 확인한 뒤 다시 되돌려 주세요.")
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("다른 화면에서 공개 상태가 변경되었습니다. 최신 상태를 확인한 뒤 다시 시도하세요.")

            if strategy == "immediate":
                # A hold overrides automatic release for this division only;
                # resuming uses live scores, never a final-score snapshot.
                if release is None or release.mode == "not_started":
                    if action != "undo":
                        raise ValueError("이미 모든 성적이 자동 공개되고 있습니다.")
                    if release is None:
                        release = ScoreboardReleaseRow(contest_id=contest_id, division_id=division_id)
                        db.add(release)
                    self._reset_scoreboard_release(release)
                    release.mode = "partial"
                    release.strategy = "immediate"
                    release.revision = revision + 1
                elif action == "undo":
                    self._undo_scoreboard_release(release)
                    release.revision = revision + 1
                elif release.mode != "all":
                    release.undo_history = [*(release.undo_history or []), {
                        "action": "all", "mode": release.mode, "revealed_ranks": [],
                    }]
                    release.mode = "all"
                    release.revision = revision + 1
                db.commit()
                return self.scoreboard_release(contest_id, division_id)

            if action == "undo":
                if release is None:
                    raise ValueError("되돌릴 순위 공개 기록이 없습니다.")
                self._undo_scoreboard_release(release)
                release.revision = revision + 1
                db.commit()
                return self._release_summary(release, strategy, include_undo=True)

            starting = release is None or release.mode == "not_started"
            if starting:
                if action in {"rank", "next"}:
                    raise ValueError("순위 공개를 먼저 시작하세요.")
                pending = db.scalar(select(SubmissionRow.submission_id).join(
                    ParticipantTeamRow, ParticipantTeamRow.participant_team_id == SubmissionRow.participant_team_id,
                ).where(
                    SubmissionRow.contest_id == contest_id,
                    SubmissionRow.division_id == division_id,
                    SubmissionRow.submission_kind == "participant",
                    ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
                    SubmissionRow.status.in_(["waiting", "preparing", "judging"]),
                ).limit(1))
                if pending:
                    raise ValueError("이 유형의 채점이 끝난 뒤 순위 공개를 시작하세요.")
                board = self.scoreboard_rows(contest_id, division_id, public_view=False)
                if release is None:
                    release = ScoreboardReleaseRow(contest_id=contest_id, division_id=division_id, revision=0)
                    db.add(release)
                release.mode = "partial"
                release.strategy = strategy
                release.snapshot_rows = jsonable_encoder(board["rows"])
                release.revealed_ranks = []
                release.resolver_state = self._new_resolver_state(db, contest, division_id) if strategy == "resolver" else None
                release.undo_history = []
                entry = {"action": action, "mode": "not_started"}
            else:
                if action == "start":
                    return self._release_summary(release, include_undo=True)
                if action == "next" and expected_step is not None and expected_step != release.resolver_state["step"]:
                    raise ValueError("다른 화면에서 공개가 진행되었습니다. 새로고침 후 다시 시도하세요.")
                if (action == "all" and release.mode == "all") or (action == "rank" and rank in release.revealed_ranks) or (action == "next" and not release.resolver_state["pending"]):
                    return self._release_summary(release, include_undo=True)
                entry = {"action": action, "mode": release.mode, "revealed_ranks": list(release.revealed_ranks),
                         "legacy_base": release.undo_history is None}
                if action == "rank":
                    entry["rank"] = rank
                if strategy == "resolver" and action == "all":
                    entry["resolver_state"] = deepcopy(release.resolver_state)

            ranks = {row["rank"] for row in release.snapshot_rows}
            if strategy == "resolver":
                if action == "next":
                    entry["resolver_delta"] = self._advance_resolver(release)
                if action == "all":
                    state = deepcopy(release.resolver_state)
                    state.update(pending=[], step=state["total_steps"], last_event=None)
                    release.resolver_state = state
                if not release.resolver_state["pending"]:
                    release.mode = "all"
                    release.revealed_ranks = sorted(ranks)
                else:
                    release.mode = "partial"
            else:
                revealed = set(release.revealed_ranks)
                if action == "rank":
                    if rank not in ranks:
                        raise ValueError("공개할 순위가 없습니다.")
                    revealed.add(rank)
                elif action == "all":
                    revealed = ranks
                release.revealed_ranks = sorted(revealed)
                release.mode = "all" if ranks <= revealed else "partial"
            release.undo_history = [*(release.undo_history or []), entry]
            release.revision = revision + 1
            db.commit()
            return self._release_summary(release, include_undo=True)

    @staticmethod
    def _public_scoreboard_row(original: dict) -> dict:
        row = {**original, "is_revealed": True, "last_solved_at": None}
        if not settings.feature_public_scoreboard_penalty:
            row["penalty"] = None
        row["problem_scores"] = [{
            **score, "penalty": None, "solved_at": None,
            "best_submission_id": None, "best_submitted_at": None,
        } for score in original["problem_scores"]]
        return row

    def _released_scoreboard(self, release: ScoreboardReleaseRow) -> dict:
        if release.strategy == "resolver":
            state = release.resolver_state
            originals = release.snapshot_rows if release.mode == "all" else state["rows"]
            pending = state["pending"]
            pending_teams = {event["team_id"] for event in pending}
            pending_counts: dict[tuple[str, str], int] = {}
            for event in pending:
                key = (event["team_id"], event["problem_id"])
                pending_counts[key] = pending_counts.get(key, 0) + 1
            rows = []
            for original in originals:
                row = self._public_scoreboard_row(original)
                row["is_finalized"] = row["team_id"] not in pending_teams
                for score in row["problem_scores"]:
                    score["pending_attempts"] = pending_counts.get((row["team_id"], score["problem_id"]), 0)
                rows.append(row)
        else:
            revealed = set(release.revealed_ranks)
            rows = []
            for index, original in enumerate(release.snapshot_rows):
                if original["rank"] not in revealed:
                    rows.append({
                        "rank": original["rank"], "team_id": f"unrevealed-{index}",
                        "team_name": "아직 공개되지 않은 순위", "division_id": release.division_id,
                        "division": None, "solved": 0, "penalty": None,
                        "submission_count": 0, "problem_scores": [], "is_revealed": False,
                    })
                    continue
                rows.append(self._public_scoreboard_row(original))
        return {"frozen": release.mode != "all", "rows": rows, "release": self._release_summary(release)}

    def scoreboard_rows(self, contest_id: str, division_id: str | None = None, public_view: bool = False, *, _cutoff_at: datetime | None = None, serialized: bool = False, response_division=None) -> dict | str | None:
        compute = lambda: self._scoreboard_rows_uncached(contest_id, division_id, public_view, _cutoff_at=_cutoff_at)
        if response_division is not None:
            raw_compute = compute
            def compute():
                board = raw_compute()
                return {"data": {"division": response_division.model_dump(mode="json"), **board}} if board else None
        if _cutoff_at is not None:
            value = compute()
            return json.dumps(jsonable_encoder(value), separators=(",", ":")) if serialized else value
        # Read the current visibility/freeze policy before every cache lookup.
        # A time boundary or settings change must never reuse a live public board.
        with self._session() as db:
            contest = db.get(ContestRow, contest_id)
            if not contest:
                return None
            ended = self._scoreboard_ended(contest)
            release = db.get(ScoreboardReleaseRow, division_id) if public_view and division_id and ended else None
            strategy = contest.scoreboard_release_mode or "manual"
            immediate_hold = bool(ended and strategy == "immediate" and release and release.mode == "partial")
            cutoff = None
            if public_view and (immediate_hold or not (ended and strategy == "immediate")):
                cutoff = self._scoreboard_cutoff(contest, now_utc(), resolver=immediate_hold or (ended and strategy == "resolver"))
            identity = [contest_id, division_id, public_view, ended, cutoff, settings.feature_public_scoreboard_penalty,
                        [contest.status, contest.start_at, contest.end_at, contest.freeze_at,
                         contest.scoreboard_freeze_mode, contest.scoreboard_frozen_at,
                         contest.scoreboard_release_mode],
                        [release.mode, release.strategy, release.revision] if release else None]
        return cached_result(f"scoreboard:{contest_id}", identity, settings.scoreboard_cache_ttl_seconds,
            compute, serialized=serialized, snapshot=serialized and not ended, compressed=response_division is not None)

    def _scoreboard_rows_uncached(self, contest_id: str, division_id: str | None = None, public_view: bool = False, *, _cutoff_at: datetime | None = None) -> dict | None:
        with self._session() as db:
            contest = db.info.get("request_contest_policies", {}).get(contest_id) or db.get(ContestRow, contest_id)
            if not contest:
                return None

            ended = self._scoreboard_ended(contest)
            strategy = contest.scoreboard_release_mode or "manual"
            release = None
            if public_view and division_id:
                release = db.get(ScoreboardReleaseRow, division_id) if ended else None
                if release and release.mode != "not_started" and strategy != "immediate":
                    return self._released_scoreboard(release)

            cutoff_at = _cutoff_at
            frozen = False
            now = now_utc()
            immediate_hold = bool(ended and strategy == "immediate" and release and release.mode == "partial")
            if public_view and (immediate_hold or not (ended and strategy == "immediate")):
                cutoff_at = self._scoreboard_cutoff(contest, now, resolver=immediate_hold or (ended and strategy == "resolver"))
                frozen = cutoff_at is not None

            team_filters = [ParticipantTeamRow.contest_id == contest_id]
            problem_filters = [ProblemRow.contest_id == contest_id]
            submission_filters = [
                SubmissionRow.contest_id == contest_id,
                SubmissionRow.submission_kind == "participant",
            ]
            if division_id:
                team_filters.append(ParticipantTeamRow.division_id == division_id)
                problem_filters.append(ProblemRow.division_id == division_id)
                submission_filters.append(SubmissionRow.division_id == division_id)
            if cutoff_at:
                submission_filters.append(SubmissionRow.submitted_at <= cutoff_at)

            teams = db.execute(select(ParticipantTeamRow.participant_team_id, ParticipantTeamRow.team_name,
                ParticipantTeamRow.division_id).where(*team_filters).order_by(ParticipantTeamRow.team_name)).all()
            excluded_team_ids = {team.participant_team_id for team in teams if team.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX)}
            teams = [team for team in teams if team.participant_team_id not in excluded_team_ids]
            problems = db.execute(select(ProblemRow.problem_id, ProblemRow.problem_code)
                .where(*problem_filters).order_by(ProblemRow.display_order)).all()
            divisions = {
                row.division_id: _division(row)
                for row in db.scalars(select(ContestDivisionRow).where(ContestDivisionRow.contest_id == contest_id)).all()
            }
            problem_by_id = {problem.problem_id: problem for problem in problems}
            submissions = db.execute(select(
                SubmissionRow.submission_id, SubmissionRow.participant_team_id,
                SubmissionRow.problem_id, SubmissionRow.status, SubmissionRow.submitted_at,
            ).where(*submission_filters).order_by(SubmissionRow.submitted_at, SubmissionRow.submission_id)).all()
            if excluded_team_ids:
                submissions = [submission for submission in submissions if submission.participant_team_id not in excluded_team_ids]

            team_name_by_id = {team.participant_team_id: team.team_name for team in teams}
            problem_stats_by_id = {
                problem.problem_id: {
                    "problem_id": problem.problem_id,
                    "problem_code": problem.problem_code,
                    "total_submissions": 0,
                    "accepted_submissions": 0,
                    "accepted_team_ids": set(),
                    "first_accepted_team_id": None,
                    "first_accepted_team_name": None,
                    "first_accepted_at": None,
                    "first_accepted_elapsed_minutes": None,
                }
                for problem in problems
            }
            submission_count_by_team: dict[str, int] = {}
            accepted_by_team_problem: dict[tuple[str, str], dict] = {}
            problem_attempts_by_team: dict[tuple[str, str], dict] = {}
            # Infrastructure failures and compilation errors are not wrong attempts.
            penalty_statuses = {
                SubmissionStatus.WRONG_ANSWER.value,
                SubmissionStatus.RUNTIME_ERROR.value,
                SubmissionStatus.TIME_LIMIT_EXCEEDED.value,
                SubmissionStatus.MEMORY_LIMIT_EXCEEDED.value,
                SubmissionStatus.OUTPUT_LIMIT_EXCEEDED.value,
            }
            tracked_statuses = {
                SubmissionStatus.ACCEPTED.value,
                *penalty_statuses,
            }
            for submission in submissions:
                submission_count_by_team[submission.participant_team_id] = submission_count_by_team.get(submission.participant_team_id, 0) + 1
                if submission.problem_id not in problem_by_id:
                    continue
                problem_stats = problem_stats_by_id.get(submission.problem_id)
                if problem_stats is not None:
                    problem_stats["total_submissions"] += 1
                    if submission.status == SubmissionStatus.ACCEPTED.value:
                        solved_at = _aware(submission.submitted_at)
                        elapsed_minutes = max(0, int((solved_at - _aware(contest.start_at)).total_seconds() // 60))
                        problem_stats["accepted_submissions"] += 1
                        problem_stats["accepted_team_ids"].add(submission.participant_team_id)
                        first_accepted_at = problem_stats["first_accepted_at"]
                        if first_accepted_at is None or solved_at < first_accepted_at:
                            problem_stats["first_accepted_team_id"] = submission.participant_team_id
                            problem_stats["first_accepted_team_name"] = (
                                team_name_by_id.get(submission.participant_team_id)
                                or submission.participant_team_id
                            )
                            problem_stats["first_accepted_at"] = solved_at
                            problem_stats["first_accepted_elapsed_minutes"] = elapsed_minutes
                key = (submission.participant_team_id, submission.problem_id)
                stats = problem_attempts_by_team.setdefault(
                    key,
                    {
                        "attempts": 0,
                        "solved": False,
                        "wrong_before_solved": 0,
                    },
                )
                if stats["solved"] or submission.status not in tracked_statuses:
                    continue
                if submission.status in penalty_statuses:
                    stats["attempts"] += 1
                    continue
                if submission.status == SubmissionStatus.ACCEPTED.value:
                    stats["solved"] = True
                    stats["wrong_before_solved"] = stats["attempts"]
                    solved_at = _aware(submission.submitted_at)
                    elapsed_minutes = max(0, int((solved_at - _aware(contest.start_at)).total_seconds() // 60))
                    accepted_by_team_problem[key] = {
                        "submission_id": submission.submission_id,
                        "submitted_at": solved_at,
                        "elapsed_minutes": elapsed_minutes,
                        "penalty": elapsed_minutes + stats["wrong_before_solved"] * 20,
                        "status": submission.status,
                    }

            rows = []
            for team in teams:
                problem_scores = []
                total_penalty = 0
                solved = 0
                last_solved_at = None
                unresolved_attempts = 0
                for problem in problems:
                    accepted = accepted_by_team_problem.get((team.participant_team_id, problem.problem_id))
                    stats = problem_attempts_by_team.get((team.participant_team_id, problem.problem_id))
                    solved_problem = accepted is not None
                    if solved_problem:
                        solved += 1
                        total_penalty += int(accepted["penalty"])
                    if accepted and (last_solved_at is None or accepted["submitted_at"] > last_solved_at):
                        last_solved_at = accepted["submitted_at"]
                    attempts = int(stats["attempts"]) if stats else 0
                    wrong_attempts = int(stats["wrong_before_solved"]) if solved_problem and stats else attempts
                    if not solved_problem:
                        unresolved_attempts += attempts
                    problem_scores.append(
                        {
                            "problem_id": problem.problem_id,
                            "problem_code": problem.problem_code,
                            "attempts": attempts,
                            "wrong_attempts": wrong_attempts,
                            "solved": solved_problem,
                            "penalty": accepted["penalty"] if accepted else None,
                            "solved_at": accepted["submitted_at"] if accepted else None,
                            "best_submission_id": accepted["submission_id"] if accepted else None,
                            "best_submitted_at": accepted["submitted_at"] if accepted else None,
                            "best_status": accepted["status"] if accepted else None,
                        }
                    )
                division = divisions.get(team.division_id)
                rows.append(
                    {
                        "team_id": team.participant_team_id,
                        "team_name": team.team_name,
                        "division_id": team.division_id,
                        "division": division.name if division else None,
                        "solved": solved,
                        "penalty": total_penalty,
                        "submission_count": submission_count_by_team.get(team.participant_team_id, 0),
                        "last_solved_at": last_solved_at,
                        "problem_scores": problem_scores,
                        "_unresolved_attempts": unresolved_attempts,
                    }
                )

            def rank_key(row: dict) -> tuple:
                return (-row["solved"], row["penalty"], row["_unresolved_attempts"])

            rows.sort(
                key=lambda row: (
                    *rank_key(row),
                    row["last_solved_at"] or datetime.max.replace(tzinfo=timezone.utc),
                    row["team_name"],
                )
            )
            previous_key = None
            current_rank = 0
            for index, row in enumerate(rows, start=1):
                current_key = rank_key(row)
                if current_key != previous_key:
                    current_rank = index
                    previous_key = current_key
                row["rank"] = current_rank
                row.pop("_unresolved_attempts", None)
                if public_view:
                    if not settings.feature_public_scoreboard_penalty:
                        row["penalty"] = None
                    row["last_solved_at"] = None
                    for problem_score in row["problem_scores"]:
                        problem_score["penalty"] = None
                        problem_score["solved_at"] = None
                        problem_score["best_submission_id"] = None
                        problem_score["best_submitted_at"] = None

            if public_view and frozen and strategy == "resolver":
                # Counts must depend only on received submissions, never on
                # their unrevealed verdicts (including submissions after an AC).
                hidden_filters = [
                    SubmissionRow.contest_id == contest_id,
                    SubmissionRow.submission_kind == "participant",
                    SubmissionRow.submitted_at > cutoff_at,
                ]
                if division_id:
                    hidden_filters.append(SubmissionRow.division_id == division_id)
                hidden_counts = {
                    (team_id, problem_id): count
                    for team_id, problem_id, count in db.execute(select(
                        SubmissionRow.participant_team_id, SubmissionRow.problem_id, func.count(),
                    ).where(*hidden_filters).group_by(SubmissionRow.participant_team_id, SubmissionRow.problem_id))
                }
                for row in rows:
                    for score in row["problem_scores"]:
                        score["pending_attempts"] = 0 if score["solved"] else hidden_counts.get((row["team_id"], score["problem_id"]), 0)

            problem_stats = []
            total_team_count = len(teams)
            for problem in problems:
                stats = problem_stats_by_id[problem.problem_id]
                accepted_team_count = len(stats["accepted_team_ids"])
                total_submissions = int(stats["total_submissions"])
                accepted_submissions = int(stats["accepted_submissions"])
                problem_stats.append(
                    {
                        "problem_id": stats["problem_id"],
                        "problem_code": stats["problem_code"],
                        "total_submissions": total_submissions,
                        "accepted_submissions": accepted_submissions,
                        "accepted_team_count": accepted_team_count,
                        "total_team_count": total_team_count,
                        "acceptance_rate": round(accepted_submissions / total_submissions * 100, 1)
                        if total_submissions
                        else None,
                        "first_accepted_team_id": stats["first_accepted_team_id"],
                        "first_accepted_team_name": stats["first_accepted_team_name"],
                        "first_accepted_at": stats["first_accepted_at"],
                        "first_accepted_elapsed_minutes": stats["first_accepted_elapsed_minutes"],
                    }
                )

            result = {"frozen": frozen, "rows": rows}
            if not public_view:
                result["problem_stats"] = problem_stats
            else:
                result["release"] = (self._immediate_release_summary(rows) if ended and strategy == "immediate" and not immediate_hold else
                    self._release_summary(release, strategy))
                if strategy == "immediate" and release:
                    result["release"].update(total_count=len(rows), revealed_count=0 if immediate_hold else len(rows))
            return result

    def _mail_exists(self, db: Session, mail_type: str, recipient_email: str, subject: str, contest_id: str | None = None) -> bool:
        # Unknown legacy scope must not cause old invites/reminders to resend.
        return (
            db.scalar(
                select(MailQueueItemRow.mail_queue_id)
                .where(
                    MailQueueItemRow.mail_type == mail_type,
                    MailQueueItemRow.recipient_email == recipient_email,
                    MailQueueItemRow.subject == subject,
                    or_(MailQueueItemRow.contest_id == contest_id, MailQueueItemRow.contest_id.is_(None)),
                    MailQueueItemRow.status.in_(["pending", "sending", "sent"]),
                )
                .limit(1)
            )
            is not None
        )

    def enqueue_mail(
        self,
        mail_type: str,
        recipient_email: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        dedupe: bool = False,
        *,
        contest_id: str | None = None,
        delivery_id: str | None = None,
    ) -> MailQueueItem:
        with self._session() as db:
            def existing_delivery():
                existing = db.get(MailQueueItemRow, delivery_id) if delivery_id else None
                if existing and (existing.mail_type != mail_type or existing.recipient_email != recipient_email
                                 or existing.subject != subject or existing.body_text != body_text):
                    raise AppError(409, "mail_event_conflict", "이미 처리한 메일 요청과 내용이 다릅니다.")
                return existing

            existing = existing_delivery()
            if existing:
                return _mail(existing)
            internal_recipient = is_internal_mail_recipient(recipient_email)
            if dedupe and self._mail_exists(db, mail_type, recipient_email, subject, contest_id):
                existing = db.scalar(
                    select(MailQueueItemRow)
                    .where(
                        MailQueueItemRow.mail_type == mail_type,
                        MailQueueItemRow.recipient_email == recipient_email,
                        MailQueueItemRow.subject == subject,
                        or_(MailQueueItemRow.contest_id == contest_id, MailQueueItemRow.contest_id.is_(None)),
                        MailQueueItemRow.status.in_(["pending", "sending", "sent"]),
                    )
                    .order_by(MailQueueItemRow.created_at.desc())
                    .limit(1)
                )
                return _mail(existing)
            row = MailQueueItemRow(
                mail_queue_id=delivery_id or new_id(),
                contest_id=contest_id,
                mail_type=mail_type,
                recipient_email=recipient_email,
                subject=subject,
                body_text=body_text,
                body_html=body_html or render_basic_html(subject, body_text),
                status="canceled" if internal_recipient else "pending",
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = existing_delivery()
                if not existing:
                    raise
                return _mail(existing)
            db.refresh(row)
            return _mail(row)

    def _cancel_pending_participant_mail_rows(self, db: Session, emails: list[str]) -> int:
        normalized = sorted({email.strip().lower() for email in emails if email.strip()})
        if not normalized:
            return 0
        rows = db.scalars(
            select(MailQueueItemRow).where(
                func.lower(MailQueueItemRow.recipient_email).in_(normalized),
                MailQueueItemRow.status.in_(["pending", "sending"]),
            )
        ).all()
        for row in rows:
            row.status = "canceled"
        return len(rows)

    def enqueue_due_contest_reminders(self) -> int:
        reminder_windows = [
            ("24h", timedelta(hours=24), "24시간"),
            ("1h", timedelta(hours=1), "1시간"),
            ("10m", timedelta(minutes=10), "10분"),
        ]
        now = now_utc()
        queued_count = 0
        with self._session() as db:
            contests = db.scalars(
                select(ContestRow).where(
                    ContestRow.status.in_([ContestStatus.OPEN.value, ContestStatus.RUNNING.value]),
                    ContestRow.start_at > now,
                    ContestRow.start_at <= now + timedelta(hours=24),
                )
            ).all()
            for contest in contests:
                starts_in = _aware(contest.start_at) - now
                for code, window, label in reminder_windows:
                    if starts_in < timedelta(0) or starts_in > window:
                        continue
                    teams = db.scalars(
                        select(ParticipantTeamRow)
                        .options(selectinload(ParticipantTeamRow.members))
                        .where(
                            ParticipantTeamRow.contest_id == contest.contest_id,
                            ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX),
                        )
                    ).all()
                    for team in teams:
                        division = db.get(ContestDivisionRow, team.division_id)
                        if not division:
                            continue
                        content = contest_reminder_mail(
                            contest_title=contest.title,
                            organization_name=contest.organization_name,
                            team_name=team.team_name,
                            division_name=division.name,
                            starts_at=_aware(contest.start_at),
                            remaining_label=label,
                            contest_url=absolute_url(f"/contests/{contest.contest_id}"),
                        )
                        mail_type = f"contest_reminder_{code}"
                        for member in team.members:
                            if is_internal_mail_recipient(member.email):
                                continue
                            if self._mail_exists(db, mail_type, member.email, content.subject, contest.contest_id):
                                continue
                            db.add(
                                MailQueueItemRow(
                                    mail_type=mail_type,
                                    contest_id=contest.contest_id,
                                    recipient_email=member.email,
                                    subject=content.subject,
                                    body_text=content.body_text,
                                    body_html=content.body_html,
                                )
                            )
                            queued_count += 1
            db.commit()
        return queued_count

    def enqueue_due_contest_emergency_notices(self) -> int:
        if not settings.feature_emergency_notice_auto:
            return 0

        # A newly announced start takes banner priority over reminders for a
        # short contest's later milestones. All events remain in notice history.
        targets = [("freeze", "freeze_at"), ("end", "end_at"), ("start", "start_at")]
        event_window = timedelta(minutes=10)
        now = now_utc()
        due: list[tuple[str, str, str, str]] = []

        with self._session() as db:
            contests = db.scalars(
                select(ContestRow).where(
                    ContestRow.status.in_(
                        [
                            ContestStatus.SCHEDULED.value,
                            ContestStatus.OPEN.value,
                            ContestStatus.RUNNING.value,
                            ContestStatus.ENDED.value,
                        ]
                    )
                )
            ).all()
            for contest in contests:
                start_at, end_at = _aware(contest.start_at), _aware(contest.end_at)
                previous = db.scalars(select(ContestNoticeRow).where(
                    ContestNoticeRow.contest_id == contest.contest_id,
                    ContestNoticeRow.emergency.is_(True),
                )).all()
                for target, field_name in targets:
                    if target == "start":
                        # Do not announce a past start after the contest ended,
                        # including contests ended manually ahead of schedule.
                        if contest.status == ContestStatus.ENDED.value or (end_at and now >= end_at):
                            continue
                    elif start_at and now < start_at:
                        # Before opening, only announce the upcoming start.
                        continue
                    if target == "freeze" and contest.scoreboard_freeze_mode == ScoreboardFreezeMode.LIVE.value:
                        continue
                    target_at = _aware(getattr(contest, field_name))
                    if not target_at:
                        continue
                    remaining = target_at - now
                    if not (-event_window <= remaining <= timedelta(minutes=30)):
                        continue
                    if remaining > timedelta(0) and contest.status == ContestStatus.ENDED.value:
                        continue
                    title = f"{COUNTDOWN_LABELS[target]} 안내"
                    body = countdown_template(target, target_at)
                    notice_id = scheduled_notice_id(contest.contest_id, f"countdown:{target}", target_at)
                    old_copies = [scheduled_notice_copy(target, label) for label in ["30분", "10분", "5분", "1분", None]]
                    known_ids = {notice_id} | {scheduled_notice_id(contest.contest_id, old_title, target_at) for old_title, _ in old_copies}
                    matching = []
                    for notice in previous:
                        legacy = legacy_scheduled_notice(notice.body)
                        same_legacy_event = legacy and legacy[0] == notice.title and scheduled_notice_id(contest.contest_id, legacy[0], legacy[1]) in known_ids
                        if notice.contest_notice_id in known_ids or notice.body == body or same_legacy_event:
                            matching.append(notice)
                    if matching:
                        # Upgrade an already-issued automatic reminder in place.
                        # Keep its ID, publication time, manual edits and any newer banner.
                        notice = max(matching, key=lambda item: _aware(item.published_at))
                        previous_body = notice.body
                        standard = previous_body in {copy for _, copy in old_copies} or legacy_scheduled_notice(previous_body)
                        if standard:
                            db.execute(update(ContestNoticeRow).where(
                                ContestNoticeRow.contest_notice_id == notice.contest_notice_id,
                                ContestNoticeRow.body == previous_body,
                            ).values(title=title, body=body))
                            db.execute(update(ContestRow).where(
                                ContestRow.contest_id == contest.contest_id,
                                ContestRow.emergency_notice == previous_body,
                            ).values(emergency_notice=body))
                        continue
                    due.append((contest.contest_id, title, body, notice_id))
            db.commit()

        queued_count = 0
        for contest_id, title, body, notice_id in due:
            with self._session() as db:
                if not db.get(ContestRow, contest_id) or db.get(ContestNoticeRow, notice_id):
                    continue
            try:
                self.create_contest_notice(
                    contest_id,
                    title,
                    body,
                    pinned=True,
                    emergency=True,
                    visibility="participants",
                    notice_id=notice_id,
                )
            except IntegrityError:
                # Another worker may have inserted the same scheduled event.
                with self._session() as db:
                    if db.get(ContestNoticeRow, notice_id):
                        continue
                raise
            queued_count += 1
        return queued_count

    def enqueue_bundle_warm(self, contest_id: str, problem_id: str) -> None:
        with self._session() as db:
            duplicate_pending = db.scalar(
                select(BundleWarmQueueItemRow.bundle_warm_queue_id).where(
                    BundleWarmQueueItemRow.contest_id == contest_id,
                    BundleWarmQueueItemRow.problem_id == problem_id,
                    BundleWarmQueueItemRow.status.in_(["pending", "running"]),
                )
            )
            if duplicate_pending:
                return
            db.add(BundleWarmQueueItemRow(contest_id=contest_id, problem_id=problem_id))
            db.commit()

    def create_otp(self, contest_id: str, email: str) -> str:
        if is_internal_mail_recipient(email):
            raise ValueError("team member not registered")
        team = self.get_team_by_email(contest_id, email)
        if not team:
            raise ValueError("team member not registered")
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._session() as db:
            row = db.get(OtpCodeRow, email)
            if row:
                row.contest_id = team.contest_id
                row.code = code
                row.created_at = now_utc()
                row.expires_at = now_utc() + timedelta(seconds=settings.otp_ttl_seconds)
                row.verified_at = None
            else:
                db.add(
                    OtpCodeRow(
                        email=email,
                        contest_id=team.contest_id,
                        code=code,
                        expires_at=now_utc() + timedelta(seconds=settings.otp_ttl_seconds),
                    )
                )
            content = login_verification_mail(code=code, ttl_seconds=settings.otp_ttl_seconds, audience="participant")
            db.add(
                MailQueueItemRow(
                    mail_type="participant_otp",
                    contest_id=contest_id,
                    recipient_email=email,
                    subject=content.subject,
                    body_text=content.body_text,
                    body_html=content.body_html,
                )
            )
            db.commit()
        return code

    def participant_otp_retry_after_seconds(self, contest_id: str, email: str) -> int:
        with self._session() as db:
            row = db.get(OtpCodeRow, email)
            return self._otp_retry_after_seconds(row, contest_id)

    def _otp_retry_after_seconds(self, row: OtpCodeRow | None, scope: str) -> int:
        if not row or row.contest_id != scope:
            return 0
        created_at = _aware(row.created_at)
        if not created_at:
            return 0
        available_at = created_at + timedelta(seconds=settings.otp_request_cooldown_seconds)
        remaining = (available_at - now_utc()).total_seconds()
        if remaining <= 0:
            return 0
        return max(1, math.ceil(remaining))

    def provision_node(self, node_name: str, node_secret: str, total_slots: int = 10, agent_version: str = "unknown") -> JudgeNode:
        """Create a credential through the trusted administrator CLI, never HTTP."""
        if not node_name or len(node_name) > 120 or not node_secret or not 1 <= total_slots <= 1024:
            raise ValueError("invalid node configuration")
        with self._session() as db:
            if db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.node_name == node_name)):
                raise ValueError("node already provisioned")
            row = JudgeNodeRow(
                node_name=node_name, node_secret_hash=hash_password(node_secret),
                total_slots=total_slots, free_slots=total_slots, agent_version=agent_version,
                # Provisioning is not a heartbeat.
                last_heartbeat_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _node(row)

    @staticmethod
    def _check_node_secret(node: JudgeNodeRow | None, node_secret: str) -> None:
        if not valid_node_credential(node, node_secret):
            raise ValueError("node secret mismatch")

    def register_node(self, node_name: str, node_secret: str, total_slots: int, agent_version: str = "unknown", execution_capabilities: list[str] | None = None, execution_revision: str | None = None) -> JudgeNode:
        with self._session() as db:
            row = db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.node_name == node_name))
            self._check_node_secret(row, node_secret)
            row.total_slots = total_slots
            row.free_slots = total_slots
            row.agent_version = agent_version or "unknown"
            row.execution_capabilities = execution_capabilities or []
            row.execution_revision = execution_revision
            row.last_heartbeat_at = now_utc()
            db.commit()
            db.refresh(row)
            return _node(row)

    def verify_node_secret(self, node_id: str, node_secret: str) -> bool | None:
        with self._session() as db:
            row = db.get(JudgeNodeRow, node_id)
            if not row:
                return None
            return valid_node_credential(row, node_secret)

    def append_judge_agent_logs(
        self,
        node_id: str,
        node_secret: str,
        logs: list[dict],
        *,
        keep_per_node: int = 5000,
    ) -> int | None:
        with self._session() as db:
            node = db.get(JudgeNodeRow, node_id)
            if not node:
                return None
            self._check_node_secret(node, node_secret)
            accepted = 0
            for item in logs[-300:]:
                message = str(item.get("message") or "").strip("\r\n")
                if not message:
                    continue
                level = str(item.get("level") or "info").strip().lower()[:16] or "info"
                db.add(
                    JudgeAgentLogRow(
                        judge_node_id=node.judge_node_id,
                        node_name=node.node_name,
                        level=level,
                        message=message[-8000:],
                    )
                )
                accepted += 1
            db.flush()
            stale_ids = db.scalars(
                select(JudgeAgentLogRow.judge_agent_log_id)
                .where(JudgeAgentLogRow.judge_node_id == node_id)
                .order_by(JudgeAgentLogRow.created_at.desc(), JudgeAgentLogRow.judge_agent_log_id.desc())
                .offset(max(1, keep_per_node))
            ).all()
            if stale_ids:
                db.execute(
                    delete(JudgeAgentLogRow).where(
                        JudgeAgentLogRow.judge_agent_log_id.in_(stale_ids)
                    )
                )
            db.commit()
            return accepted

    def update_node_heartbeat(
        self,
        node_id: str,
        node_secret: str,
        total_slots: int,
        free_slots: int,
        running_job_count: int,
        agent_version: str | None = None,
    ) -> JudgeNode | None:
        with self._session() as db:
            row = db.get(JudgeNodeRow, node_id)
            if not row:
                return None
            self._check_node_secret(row, node_secret)
            self._recover_expired_judge_leases(db)
            row.total_slots = total_slots
            row.free_slots = free_slots
            row.running_job_count = running_job_count
            if agent_version:
                row.agent_version = agent_version
            row.last_heartbeat_at = now_utc()
            db.commit()
            db.refresh(row)
            return _node(row)

    def claim_jobs(self, node_id: str, node_secret: str, max_count: int) -> list[dict] | None:
        with self._session() as db:
            node = db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.judge_node_id == node_id).with_for_update())
            if not node:
                return None
            self._check_node_secret(node, node_secret)
            self._recover_expired_judge_leases(db)
            db.flush()
            from app.services.editor_execution import available_slots
            max_count = min(max_count, available_slots(db, node)) if node.allow_zoj else 0
            if max_count <= 0:
                db.commit()
                return []
            jobs = []
            safe_max_count = max(1, min(max_count, settings.judge_claim_max_batch_size))
            from app.orm_models import VerificationTrialRow
            is_ai_trial = select(VerificationTrialRow.submission_id).where(
                VerificationTrialRow.submission_id == JudgeJobRow.submission_id
            ).exists()
            from app.orm_models import VerificationExecutionRow
            from app.services import verification_execution
            full_verification = select(VerificationExecutionRow.submission_id).where(
                VerificationExecutionRow.submission_id == JudgeJobRow.submission_id).exists()
            rows = db.scalars(
                select(JudgeJobRow)
                .join(SubmissionRow, SubmissionRow.submission_id == JudgeJobRow.submission_id)
                .where(JudgeJobRow.status == JudgeJobStatus.PENDING.value)
                .where(~full_verification if verification_execution.CAPABILITY not in (node.execution_capabilities or []) else True)
                .where(SubmissionRow.language.in_(LANGUAGES if ICPC_CAPABILITY in (node.execution_capabilities or []) else [lang for lang in LANGUAGES if lang not in NEW_LANGUAGES]))
                .order_by(sql_case((is_ai_trial, 2), (rejudging.is_job(), 1), else_=0), JudgeJobRow.queue_position)
                .limit(safe_max_count)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.status = JudgeJobStatus.RUNNING.value
                row.assigned_node_id = node_id
                row.lease_token = new_id()
                row.leased_at = now_utc()
                submission = db.get(SubmissionRow, row.submission_id)
                if submission:
                    rejudging_item = rejudging.item_for_job(db, row.judge_job_id)
                    if rejudging_item:
                        jobs.append(rejudging.claim_payload(db, row, submission, rejudging_item))
                        continue
                    # Avoid mixing package versions or waiting for a parent
                    # lock while holding submission/job locks.
                    problem = db.scalar(select(ProblemRow).where(
                        ProblemRow.problem_id == submission.problem_id).with_for_update(skip_locked=True))
                    if problem is None and db.get(ProblemRow, submission.problem_id) is not None:
                        row.status = JudgeJobStatus.PENDING.value
                        row.assigned_node_id = row.lease_token = row.leased_at = None
                        continue
                    active_set = db.scalar(
                        select(TestcaseSetRow).where(TestcaseSetRow.problem_id == submission.problem_id, TestcaseSetRow.is_active.is_(True))
                    )
                    testcase_rows: list[TestcaseRow] = []
                    if active_set:
                        testcase_rows = db.scalars(
                            select(TestcaseRow)
                            .where(TestcaseRow.testcase_set_id == active_set.testcase_set_id)
                            .order_by(TestcaseRow.display_order)
                        ).all()
                    package_assets: list[ProblemAssetRow] = []
                    from app.services.verification_agent_tools import prepare_claim
                    testcase_rows, trial_subset, trial_valid = prepare_claim(db, submission, row, problem, testcase_rows)
                    if not trial_valid:
                        continue
                    package_role_assets: list[tuple[str, ProblemAssetRow]] = []
                    if problem:
                        package_assets = db.scalars(
                            select(ProblemAssetRow)
                            .where(ProblemAssetRow.problem_id == problem.problem_id, ProblemAssetRow.storage_key.contains("/package-files/"))
                            .order_by(ProblemAssetRow.created_at)
                        ).all()
                        for asset in package_assets:
                            if is_editor_source_asset(asset):
                                continue
                            role = None
                            for candidate in ("package-resource", "checker", "validator"):
                                if f"/package-files/{candidate}/" in asset.storage_key:
                                    role = candidate
                                    break
                            if role:
                                package_role_assets.append((role, asset))
                    bundle_url = None
                    bundle_available = False
                    if problem and active_set and not trial_subset:
                        bundle_key = self._judge_bundle_key(
                            submission.contest_id,
                            problem,
                            active_set.testcase_set_id,
                            testcase_rows,
                            package_assets,
                        )
                        bundle_available = object_storage.size_bytes(bundle_key) is not None
                        if bundle_available:
                            bundle_url = object_storage.internal_presigned_get_url(bundle_key)
                    testcases = []
                    if active_set:
                        for case in testcase_rows:
                            item = _testcase(case).model_dump(mode="json")
                            item["input_url"] = object_storage.internal_presigned_get_url(case.input_storage_key)
                            item["output_url"] = object_storage.internal_presigned_get_url(case.output_storage_key)
                            if not bundle_available:
                                input_text = None
                                output_text = None
                                inline_limit = max(0, settings.judge_claim_inline_testcase_max_bytes)
                                try:
                                    if inline_limit > 0:
                                        input_bytes = object_storage.read_bytes(case.input_storage_key)
                                        output_bytes = object_storage.read_bytes(case.output_storage_key)
                                        if len(input_bytes) <= inline_limit and len(output_bytes) <= inline_limit:
                                            input_text = input_bytes.decode("utf-8-sig")
                                            output_text = output_bytes.decode("utf-8-sig")
                                except Exception:
                                    input_text = None
                                    output_text = None
                                if input_text is not None and output_text is not None:
                                    item["input_text"] = input_text
                                    item["output_text"] = output_text
                            testcases.append(item)
                    if submission.submission_kind == "verification_trial":
                        from app.services.verification_agent_tools import probe_payload
                        testcases = probe_payload(db, submission.submission_id) or testcases
                    package_files = [
                        {
                            **_asset(asset).model_dump(mode="json"),
                            "role": role,
                            "url": object_storage.internal_presigned_get_url(asset.storage_key),
                        }
                        for role, asset in package_role_assets
                    ]
                    if submission.submission_kind == "operator_test":
                        from app.services.verification_ai import snapshot_claim
                        snapshot_claim(db, submission, problem, active_set, testcase_rows)
                    collect_all = verification_execution.claim(db, submission, node, row, active_set, testcase_rows) if submission.submission_kind == "operator_test" else False
                    submission.status = SubmissionStatus.PREPARING.value
                    submission.status_updated_at = now_utc()
                    submission.compile_message = None
                    submission.judge_message = None
                    submission.failed_testcase_order = None
                    submission.progress_current = 0 if testcases else None
                    submission.progress_total = len(testcases) if testcases else None
                    jobs.append(
                        {
                            **_job(row).model_dump(mode="json"),
                            "lease_token": row.lease_token,
                            "submission": _submission(submission).model_dump(mode="json"),
                            "problem": _problem(problem).model_dump(mode="json") if problem else None,
                            "testcase_set": _testcase_set(active_set).model_dump(mode="json") if active_set else None,
                            "testcases": testcases,
                            "package_files": package_files,
                            "bundle_url": bundle_url,
                            **({"collect_all_results": True} if collect_all else {}),
                        }
                    )
            db.commit()
            return jobs

    def _judge_bundle_key(
        self,
        contest_id: str,
        problem: ProblemRow,
        testcase_set_id: str,
        testcase_rows: list[TestcaseRow],
        package_assets: list[ProblemAssetRow],
    ) -> str:
        role_assets: list[tuple[str, ProblemAssetRow]] = []
        for asset in package_assets:
            if is_editor_source_asset(asset):
                continue
            for role in ("package-resource", "checker", "validator"):
                if f"/package-files/{role}/" in asset.storage_key:
                    role_assets.append((role, asset))
                    break
        digest = hashlib.sha256()
        digest.update(problem.problem_id.encode("utf-8"))
        digest.update(str(problem.time_limit_ms).encode("utf-8"))
        digest.update(str(problem.memory_limit_mb).encode("utf-8"))
        digest.update(json.dumps(problem.language_resource_limits or {}, sort_keys=True).encode("utf-8"))
        digest.update(testcase_set_id.encode("utf-8"))
        for case in testcase_rows:
            digest.update(case.testcase_id.encode("utf-8"))
            digest.update(case.input_sha256.encode("utf-8"))
            digest.update(case.output_sha256.encode("utf-8"))
            digest.update(json.dumps([case.display_order, case.time_limit_ms_override,
                case.memory_limit_mb_override]).encode("utf-8"))
        for role, asset in sorted(role_assets, key=lambda item: (item[0], item[1].asset_id)):
            digest.update(role.encode("utf-8"))
            digest.update(asset.asset_id.encode("utf-8"))
            digest.update(asset.sha256.encode("utf-8"))
        version_hash = digest.hexdigest()
        return f"contests/{contest_id}/problems/{problem.problem_id}/judge-bundles/{testcase_set_id}-{version_hash}.json.gz"

    def _ensure_problem_judge_bundle(
        self,
        contest_id: str,
        problem: ProblemRow,
        testcase_set: TestcaseSetRow,
        testcase_rows: list[TestcaseRow],
        package_assets: list[ProblemAssetRow],
    ) -> str:
        role_assets: list[tuple[str, ProblemAssetRow]] = []
        for asset in package_assets:
            if is_editor_source_asset(asset):
                continue
            for role in ("package-resource", "checker", "validator"):
                if f"/package-files/{role}/" in asset.storage_key:
                    role_assets.append((role, asset))
                    break
        bundle_key = self._judge_bundle_key(contest_id, problem, testcase_set.testcase_set_id, testcase_rows, package_assets)
        version_hash = bundle_key.rsplit("-", 1)[-1].replace(".json.gz", "")

        try:
            object_storage.read_bytes(bundle_key)
            return bundle_key
        except Exception:
            pass

        bundle = {
            "version_hash": version_hash,
            "problem": {
                "problem_id": problem.problem_id,
                "time_limit_ms": problem.time_limit_ms,
                "memory_limit_mb": problem.memory_limit_mb,
                "language_resource_limits": problem.language_resource_limits or {},
            },
            "testcase_set": {
                "testcase_set_id": testcase_set.testcase_set_id,
                "version": testcase_set.version,
            },
            "testcases": [],
            "package_files": [],
        }

        for case in testcase_rows:
            input_bytes = object_storage.read_bytes(case.input_storage_key)
            output_bytes = object_storage.read_bytes(case.output_storage_key)
            bundle["testcases"].append(
                {
                    "testcase_id": case.testcase_id,
                    "display_order": case.display_order,
                    "time_limit_ms_override": case.time_limit_ms_override,
                    "memory_limit_mb_override": case.memory_limit_mb_override,
                    "input_storage_key": case.input_storage_key,
                    "output_storage_key": case.output_storage_key,
                    "input_text": input_bytes.decode("utf-8-sig"),
                    "output_text": output_bytes.decode("utf-8-sig"),
                }
            )

        for role, asset in role_assets:
            blob = object_storage.read_bytes(asset.storage_key)
            bundle["package_files"].append(
                {
                    "role": role,
                    "asset_id": asset.asset_id,
                    "storage_key": asset.storage_key,
                    "original_filename": asset.original_filename,
                    "sha256": asset.sha256,
                    "inline_bytes_b64": base64.b64encode(blob).decode("ascii"),
                }
            )

        compressed = gzip.compress(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
        object_storage.write_bytes(bundle_key, compressed, "application/gzip")
        return bundle_key

    def warm_problem_judge_bundle(self, contest_id: str, problem_id: str) -> str | None:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                return None
            active_set = db.scalar(
                select(TestcaseSetRow).where(
                    TestcaseSetRow.problem_id == problem_id,
                    TestcaseSetRow.is_active.is_(True),
                )
            )
            if not active_set:
                return None
            testcase_rows = db.scalars(
                select(TestcaseRow)
                .where(TestcaseRow.testcase_set_id == active_set.testcase_set_id)
                .order_by(TestcaseRow.display_order)
            ).all()
            package_assets = db.scalars(
                select(ProblemAssetRow)
                .where(ProblemAssetRow.problem_id == problem.problem_id, ProblemAssetRow.storage_key.contains("/package-files/"))
                .order_by(ProblemAssetRow.created_at)
            ).all()
            return self._ensure_problem_judge_bundle(contest_id, problem, active_set, testcase_rows, package_assets)

    def problem_judge_bundle_status(self, contest_id: str, problem_id: str) -> dict:
        with self._session() as db:
            problem = db.get(ProblemRow, problem_id)
            if not problem or problem.contest_id != contest_id:
                raise ValueError("problem not found")
            active_set = db.scalar(
                select(TestcaseSetRow).where(
                    TestcaseSetRow.problem_id == problem_id,
                    TestcaseSetRow.is_active.is_(True),
                )
            )
            latest_queue = db.scalar(
                select(BundleWarmQueueItemRow)
                .where(
                    BundleWarmQueueItemRow.contest_id == contest_id,
                    BundleWarmQueueItemRow.problem_id == problem_id,
                )
                .order_by(BundleWarmQueueItemRow.created_at.desc())
                .limit(1)
            )
            queue = None
            if latest_queue:
                queue = {
                    "status": latest_queue.status,
                    "attempts": latest_queue.attempts,
                    "last_error": latest_queue.last_error,
                    "created_at": latest_queue.created_at.isoformat() if latest_queue.created_at else None,
                    "started_at": latest_queue.started_at.isoformat() if latest_queue.started_at else None,
                    "completed_at": latest_queue.completed_at.isoformat() if latest_queue.completed_at else None,
                }
            if not active_set:
                return {
                    "ready": False,
                    "status": "no_active_testcase_set",
                    "storage_key": None,
                    "size_bytes": None,
                    "testcase_set_id": None,
                    "version_hash": None,
                    "queue": queue,
                }
            testcase_rows = db.scalars(
                select(TestcaseRow)
                .where(TestcaseRow.testcase_set_id == active_set.testcase_set_id)
                .order_by(TestcaseRow.display_order)
            ).all()
            package_assets = db.scalars(
                select(ProblemAssetRow)
                .where(ProblemAssetRow.problem_id == problem.problem_id, ProblemAssetRow.storage_key.contains("/package-files/"))
                .order_by(ProblemAssetRow.created_at)
            ).all()
            bundle_key = self._judge_bundle_key(contest_id, problem, active_set.testcase_set_id, testcase_rows, package_assets)
            size = object_storage.size_bytes(bundle_key)
            if size is not None:
                status = "ready"
            elif latest_queue and latest_queue.status in {"pending", "running", "failed"}:
                status = latest_queue.status
            else:
                status = "missing"
            return {
                "ready": size is not None,
                "status": status,
                "storage_key": bundle_key,
                "size_bytes": size,
                "testcase_set_id": active_set.testcase_set_id,
                "version_hash": bundle_key.rsplit("-", 1)[-1].replace(".json.gz", ""),
                "queue": queue,
            }

    def claim_bundle_warm_jobs(self, limit: int = 10) -> list[tuple[str, str, str, int]]:
        with self._session() as db:
            rows = db.scalars(
                select(BundleWarmQueueItemRow)
                .where(BundleWarmQueueItemRow.status == "pending")
                .order_by(BundleWarmQueueItemRow.created_at)
                .limit(limit)
            ).all()
            claimed: list[tuple[str, str, str, int]] = []
            for row in rows:
                row.status = "running"
                row.started_at = now_utc()
                row.attempts = (row.attempts or 0) + 1
                row.last_error = None
                claimed.append((row.bundle_warm_queue_id, row.contest_id, row.problem_id, row.attempts))
            db.commit()
            return claimed

    def complete_bundle_warm_job(self, job_id: str) -> None:
        with self._session() as db:
            row = db.get(BundleWarmQueueItemRow, job_id)
            if not row:
                return
            row.status = "succeeded"
            row.completed_at = now_utc()
            row.last_error = None
            db.commit()

    def fail_bundle_warm_job(self, job_id: str, error_text: str, *, requeue: bool) -> None:
        with self._session() as db:
            row = db.get(BundleWarmQueueItemRow, job_id)
            if not row:
                return
            row.status = "pending" if requeue else "failed"
            row.last_error = (error_text or "")[:1000]
            if not requeue:
                row.completed_at = now_utc()
            db.commit()

    def _recover_expired_judge_leases(self, db: Session) -> None:
        expired_before = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds)
        rows = db.scalars(
            select(JudgeJobRow).where(
                JudgeJobRow.status == JudgeJobStatus.RUNNING.value,
                JudgeJobRow.leased_at < expired_before,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            if not row.leased_at or _aware(row.leased_at) >= expired_before:
                continue
            row.status = JudgeJobStatus.PENDING.value
            row.assigned_node_id = None
            row.lease_token = None
            row.leased_at = None
            if rejudging.requeue(db, row):
                continue
            submission = db.get(SubmissionRow, row.submission_id)
            if submission and submission.status in {SubmissionStatus.PREPARING.value, SubmissionStatus.JUDGING.value}:
                submission.status = SubmissionStatus.WAITING.value
                submission.status_updated_at = now_utc()
                submission.compile_message = None
                submission.judge_message = None
                submission.failed_testcase_order = None
                submission.progress_current = None
                submission.progress_total = None
                from app.services.verification_execution import requeue
                requeue(db, submission.submission_id)

    def _owned_running_job(self, db: Session, job_id: str, node_secret: str, lease_token: str) -> JudgeJobRow | None:
        # Serialize progress, renewal and finalization against other job writers.
        job = db.scalar(select(JudgeJobRow).where(JudgeJobRow.judge_job_id == job_id).with_for_update())
        if not job:
            return None
        node = db.get(JudgeNodeRow, job.assigned_node_id) if job.assigned_node_id else None
        self._check_node_secret(node, node_secret)
        cutoff = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds)
        if (
            job.status != JudgeJobStatus.RUNNING.value
            or not job.lease_token
            or not secrets.compare_digest(job.lease_token.encode("utf-8"), lease_token.encode("utf-8"))
            or not job.leased_at
            or _aware(job.leased_at) <= cutoff
        ):
            raise ValueError("lease mismatch")
        return job

    def update_judge_progress(
        self,
        job_id: str,
        node_secret: str,
        lease_token: str,
        status: SubmissionStatus,
        progress_current: int | None,
        progress_total: int | None,
        *, testcase_result: dict | None = None, testcase_results: list[dict] | None = None,
    ) -> tuple[Submission, JudgeJob] | None:
        if status not in JUDGE_PROGRESS_STATUSES:
            raise ValueError("invalid judge progress status")
        with self._session() as db:
            job = self._owned_running_job(db, job_id, node_secret, lease_token)
            if not job:
                return None
            submission = db.get(SubmissionRow, job.submission_id)
            if not submission:
                return None
            rejudging_item = rejudging.item_for_job(db, job.judge_job_id)
            if rejudging_item:
                rejudging.record_progress(rejudging_item, status, progress_current, progress_total)
                job.leased_at = now_utc()
                db.commit()
                return rejudging.candidate_submission(submission, rejudging_item), _job(job)
            from app.services import verification_execution
            execution = db.get(verification_execution.Execution, submission.submission_id)
            if execution:
                if progress_total not in {None, len(execution.testcases)} or (testcase_result is not None and testcase_results is not None):
                    raise ValueError("lease mismatch: invalid verification progress")
                verification_execution.merge(execution, [testcase_result] if testcase_result is not None else (testcase_results or []))
                progress_current, progress_total = len(execution.results), len(execution.testcases)
            elif testcase_result is not None or testcase_results is not None:
                raise ValueError("lease mismatch: unrequested testcase results")
            submission.status = status.value
            submission.progress_total = max(progress_total, 0) if progress_total is not None else None
            if progress_current is None:
                submission.progress_current = 0 if submission.progress_total is not None else None
            elif submission.progress_total is not None:
                submission.progress_current = min(max(progress_current, 0), submission.progress_total)
            else:
                submission.progress_current = max(progress_current, 0)
            submission.status_updated_at = now_utc()
            job.leased_at = now_utc()
            db.commit()
            db.refresh(submission)
            db.refresh(job)
            return _submission(submission), _job(job)

    def renew_judge_lease(
        self,
        job_id: str,
        node_secret: str,
        lease_token: str,
    ) -> JudgeJob | None:
        with self._session() as db:
            job = self._owned_running_job(db, job_id, node_secret, lease_token)
            if not job:
                return None
            job.leased_at = now_utc()
            db.commit()
            db.refresh(job)
            return _job(job)

    def report_judge_result(
        self,
        job_id: str,
        node_secret: str,
        lease_token: str,
        final_status: SubmissionStatus,
        compile_message: str | None,
        judge_message: str | None,
        failed_testcase_order: int | None,
        runtime_ms: int | None = None,
        memory_kb: int | None = None,
        *, testcase_results: list[dict] | None = None,
    ) -> tuple[Submission, JudgeJob] | None:
        if final_status not in JUDGE_FINAL_STATUSES:
            raise ValueError("invalid judge final status")
        with self._session() as db:
            from app.services import verification_execution
            candidate = db.scalar(select(JudgeJobRow).where(JudgeJobRow.judge_job_id == job_id).with_for_update())
            execution = db.get(verification_execution.Execution, candidate.submission_id) if candidate else None
            if candidate and execution and candidate.status == JudgeJobStatus.SUCCEEDED.value:
                node = db.get(JudgeNodeRow, candidate.assigned_node_id)
                self._check_node_secret(node, node_secret)
                existing = db.get(SubmissionRow, candidate.submission_id)
                if not execution.lease_token or not secrets.compare_digest(execution.lease_token.encode("utf-8"), lease_token.encode("utf-8")) or any([
                    existing.status != final_status.value, existing.compile_message != compile_message,
                    existing.judge_message != judge_message, existing.failed_testcase_order != failed_testcase_order]):
                    raise ValueError("lease mismatch")
                verification_execution.merge(execution, testcase_results or [], (final_status.value, failed_testcase_order))
                return _submission(existing), _job(candidate)
            job = self._owned_running_job(db, job_id, node_secret, lease_token)
            if not job:
                return None
            submission = db.get(SubmissionRow, job.submission_id)
            if not submission:
                return None
            rejudging_item = rejudging.item_for_job(db, job.judge_job_id)
            if rejudging_item:
                rejudging.record_result(rejudging_item, final_status, compile_message, judge_message,
                    failed_testcase_order, runtime_ms if settings.feature_submission_runtime_metrics else None,
                    memory_kb if settings.feature_submission_runtime_metrics else None)
                job.status = JudgeJobStatus.SUCCEEDED.value
                job.lease_token = None
                db.commit()
                return rejudging.candidate_submission(submission, rejudging_item), _job(job)
            if execution:
                verification_execution.merge(execution, testcase_results or [], (final_status.value, failed_testcase_order))
                metrics = verification_execution.summary(execution)
                runtime_ms, memory_kb = metrics["runtime_ms"], metrics["memory_kb"]
                submission.progress_current, submission.progress_total = len(execution.results), len(execution.testcases)
            elif testcase_results is not None:
                raise ValueError("lease mismatch: unrequested testcase results")
            submission.status = final_status.value
            submission.compile_message = compile_message
            submission.judge_message = judge_message
            submission.failed_testcase_order = failed_testcase_order
            if settings.feature_submission_runtime_metrics:
                submission.runtime_ms = max(runtime_ms, 0) if runtime_ms is not None else None
                submission.memory_kb = max(memory_kb, 0) if memory_kb is not None else None
            if submission.progress_total is not None and final_status == SubmissionStatus.ACCEPTED:
                submission.progress_current = submission.progress_total
            submission.status_updated_at = now_utc()
            job.status = JudgeJobStatus.SUCCEEDED.value
            job.lease_token = None
            from app.services.notifications import create_result
            create_result(db, submission, job)
            db.commit()
            db.refresh(submission)
            db.refresh(job)
            return _submission(submission), _job(job)

    def pending_mail(self, limit: int = 20) -> list[MailQueueItem]:
        with self._session() as db:
            rows = db.scalars(select(MailQueueItemRow).where(MailQueueItemRow.status == "pending").order_by(MailQueueItemRow.created_at).limit(limit)).all()
            sendable_rows = []
            for row in rows:
                if is_internal_mail_recipient(row.recipient_email):
                    row.status = "canceled"
                    continue
                sendable_rows.append(row)
            if len(sendable_rows) != len(rows):
                db.commit()
            return [_mail(row) for row in sendable_rows]

    def mark_mail_status(self, mail_queue_id: str, status: str) -> MailQueueItem | None:
        with self._session() as db:
            row = db.scalar(select(MailQueueItemRow).where(MailQueueItemRow.mail_queue_id == mail_queue_id).with_for_update())
            if not row:
                return None
            if row.status == "canceled" and status != "pending":
                return _mail(row)
            if status == "sending" and row.contest_id and db.scalar(
                select(ContestRow.deleted_at).where(ContestRow.contest_id == row.contest_id)
                .execution_options(include_deleted_contests=True)
            ) is not None:
                row.status = "canceled"
                db.commit()
                return _mail(row)
            if status == "sending" and row.status != "sending":
                row.last_attempt_at = now_utc()
            if status == "sent" and row.status != "sent":
                row.sent_at = now_utc()
            row.status = status
            db.commit()
            db.refresh(row)
            return _mail(row)

    def mark_pending_mail_sent(self) -> list[MailQueueItem]:
        sent = []
        for item in self.pending_mail():
            updated = self.mark_mail_status(item.mail_queue_id, "sent")
            if updated:
                sent.append(updated)
        return sent


store = DbStore()
