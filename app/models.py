from pydantic import model_serializer
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, EmailStr, Field
from app.problem_languages import ProblemStatements
from app.judge_languages import ProgrammingLanguages


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class ContestStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULE_TBD = "schedule_tbd"
    SCHEDULED = "scheduled"
    OPEN = "open"
    RUNNING = "running"
    ENDED = "ended"
    FINALIZED = "finalized"
    ARCHIVED = "archived"


class ContestVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class ContestResourceAccess(StrEnum):
    PRIVATE = "private"
    PARTICIPANTS = "participants"
    PUBLIC = "public"


class ScoreboardFreezeMode(StrEnum):
    AUTO = "auto"
    LIVE = "live"
    FROZEN = "frozen"


class ScoreboardReleaseMode(StrEnum):
    MANUAL = "manual"
    IMMEDIATE = "immediate"
    RESOLVER = "resolver"


class SubmissionStatus(StrEnum):
    WAITING = "waiting"
    PREPARING = "preparing"
    JUDGING = "judging"
    ACCEPTED = "accepted"
    WRONG_ANSWER = "wrong_answer"
    COMPILE_ERROR = "compile_error"
    RUNTIME_ERROR = "runtime_error"
    TIME_LIMIT_EXCEEDED = "time_limit_exceeded"
    MEMORY_LIMIT_EXCEEDED = "memory_limit_exceeded"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    SYSTEM_ERROR = "system_error"


class JudgeJobStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


JUDGE_PROGRESS_STATUSES = frozenset({SubmissionStatus.PREPARING, SubmissionStatus.JUDGING})
JUDGE_FINAL_STATUSES = frozenset({
    SubmissionStatus.ACCEPTED, SubmissionStatus.WRONG_ANSWER, SubmissionStatus.COMPILE_ERROR,
    SubmissionStatus.RUNTIME_ERROR, SubmissionStatus.TIME_LIMIT_EXCEEDED,
    SubmissionStatus.MEMORY_LIMIT_EXCEEDED, SubmissionStatus.OUTPUT_LIMIT_EXCEEDED,
    SubmissionStatus.SYSTEM_ERROR,
})


class TeamMemberRole(StrEnum):
    LEADER = "leader"
    MEMBER = "member"


class StaffAccount(BaseModel):
    staff_account_id: str = Field(default_factory=new_id)
    email: EmailStr
    display_name: str
    is_service_master: bool = False
    permissions: list[str] = Field(default_factory=list)
    contest_scopes: dict[str, list[str]] = Field(default_factory=dict)
    contest_roles: dict[str, list[str]] = Field(default_factory=dict)
    protected_master_contests: list[str] = Field(default_factory=list)


class TeamMember(BaseModel):
    team_member_id: str = Field(default_factory=new_id)
    role: TeamMemberRole
    name: str
    email: EmailStr
    active_sessions: int = 0
    last_login_at: datetime | None = None
    last_session_seen_at: datetime | None = None


class ParticipantTeam(BaseModel):
    participant_team_id: str = Field(default_factory=new_id)
    contest_id: str
    division_id: str
    team_name: str
    status: str = "invited"
    members: list[TeamMember]
    created_at: datetime = Field(default_factory=now_utc)


class Contest(BaseModel):
    contest_id: str = Field(default_factory=new_id)
    title: str
    organization_name: str
    overview: str
    status: ContestStatus
    visibility: ContestVisibility = ContestVisibility.PUBLIC
    visibility_after_end: ContestVisibility = ContestVisibility.PUBLIC
    start_at: datetime
    end_at: datetime
    freeze_at: datetime
    problem_access_after_end: ContestResourceAccess = ContestResourceAccess.PRIVATE
    scoreboard_access_after_end: ContestResourceAccess = ContestResourceAccess.PRIVATE
    submission_access_after_end: ContestResourceAccess = ContestResourceAccess.PRIVATE
    board_access_after_end: ContestResourceAccess = ContestResourceAccess.PARTICIPANTS
    board_write_after_end: bool = False
    board_question_visibility_policy: Literal["choice", "public", "private"] = "choice"
    board_question_default_visibility: Literal["public", "private"] = "public"
    board_comment_scope_policy: Literal["choice", "all", "operators"] = "choice"
    board_comment_default_scope: Literal["all", "operators"] = "all"
    notice_access_after_end: ContestResourceAccess = ContestResourceAccess.PUBLIC
    editorial_access_after_end: ContestResourceAccess = ContestResourceAccess.PRIVATE
    scoreboard_freeze_mode: ScoreboardFreezeMode = ScoreboardFreezeMode.AUTO
    scoreboard_release_mode: ScoreboardReleaseMode = ScoreboardReleaseMode.MANUAL
    scoreboard_release_locked: bool = False
    mock_judging_enabled: bool = False
    freeze_submission_count_visible: bool = False
    participant_progress_visible: bool = True
    mock_judging_progress_visible: bool = False
    emergency_notice: str | None = None
    emergency_notice_template: str | None = None
    created_at: datetime = Field(default_factory=now_utc)


class ContestDivision(BaseModel):
    division_id: str = Field(default_factory=new_id)
    contest_id: str
    code: str
    name: str
    description: str = ""
    display_order: int = 1
    created_at: datetime = Field(default_factory=now_utc)


class Problem(ProblemStatements, ProgrammingLanguages):
    problem_id: str = Field(default_factory=new_id)
    contest_id: str
    division_id: str
    problem_code: str
    title: str
    statement: str
    editorial: str = ""
    time_limit_ms: int
    memory_limit_mb: int
    language_resource_limits: dict[str, "ProblemLanguageResourceLimit"] = Field(default_factory=dict)
    display_order: int


class ProblemLanguageResourceLimit(BaseModel):
    disable_auto_adjustment: bool = Field(default=False, strict=True)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value=handler(self)
        if not self.disable_auto_adjustment: value.pop("disable_auto_adjustment", None)
        return value

    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None


class ProblemAsset(BaseModel):
    asset_id: str = Field(default_factory=new_id)
    contest_id: str
    problem_id: str
    original_filename: str
    storage_key: str
    mime_type: str
    file_size: int
    sha256: str
    source_language: str | None = None
    asset_status: str = "active"
    created_at: datetime = Field(default_factory=now_utc)


class TestcaseSet(BaseModel):
    testcase_set_id: str = Field(default_factory=new_id)
    problem_id: str
    version: int
    is_active: bool = False
    created_at: datetime = Field(default_factory=now_utc)


class Testcase(BaseModel):
    testcase_id: str = Field(default_factory=new_id)
    testcase_set_id: str
    display_order: int
    input_storage_key: str
    output_storage_key: str
    input_sha256: str
    output_sha256: str
    input_size_bytes: int | None = None
    output_size_bytes: int | None = None
    time_limit_ms_override: int | None = None
    memory_limit_mb_override: int | None = None
    created_at: datetime = Field(default_factory=now_utc)


class Submission(BaseModel):
    submission_id: str = Field(default_factory=new_id)
    contest_id: str
    division_id: str
    problem_id: str
    participant_team_id: str | None = None
    team_member_id: str | None = None
    submission_kind: str = "participant"
    submitted_by_name: str | None = None
    submitted_by_title: str | None = None
    submitted_by_email: str | None = None
    language: str
    source_code: str
    status: SubmissionStatus = SubmissionStatus.WAITING
    submitted_at: datetime = Field(default_factory=now_utc)
    status_updated_at: datetime = Field(default_factory=now_utc)
    compile_message: str | None = None
    judge_message: str | None = None
    failed_testcase_order: int | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    runtime_ms: int | None = None
    memory_kb: int | None = None


class JudgeJob(BaseModel):
    judge_job_id: str = Field(default_factory=new_id)
    submission_id: str
    contest_id: str
    division_id: str
    status: JudgeJobStatus = JudgeJobStatus.PENDING
    queue_position: int
    assigned_node_id: str | None = None
    lease_token: str | None = None
    leased_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class ServiceNotice(BaseModel):
    service_notice_id: str = Field(default_factory=new_id)
    title: str
    summary: str
    body: str
    emergency: bool = False
    published_at: datetime = Field(default_factory=now_utc)


class ContestNotice(BaseModel):
    contest_notice_id: str = Field(default_factory=new_id)
    contest_id: str
    title: str
    body: str
    body_template: str | None = None
    pinned: bool = False
    emergency: bool = False
    visibility: str = "public"
    created_by_email: EmailStr | None = None
    published_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ContestQuestionAnswer(BaseModel):
    contest_answer_id: str = Field(default_factory=new_id)
    contest_question_id: str
    contest_id: str
    body: str
    visibility: str = "public"
    created_by_email: EmailStr | None = None
    created_by_name: str | None = None
    created_by_title: str | None = None
    created_by_role: str | None = None
    created_by_team_name: str | None = None
    created_by_division_name: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ContestQuestion(BaseModel):
    contest_question_id: str = Field(default_factory=new_id)
    contest_id: str
    participant_team_id: str
    team_member_id: str
    title: str
    body: str
    visibility: str = "public"
    comment_scope: Literal["all", "operators"] = "all"
    effective_comment_scope: Literal["all", "operators"] = "all"
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    team_name: str | None = None
    division_name: str | None = None
    author_name: str | None = None
    author_email: EmailStr | None = None
    answers: list[ContestQuestionAnswer] = Field(default_factory=list)


class ContactInquiry(BaseModel):
    contact_inquiry_id: str = Field(default_factory=new_id)
    title: str
    sender_name: str
    sender_email: EmailStr
    body: str
    status: str = "pending"
    answer_body: str | None = None
    answered_by_email: EmailStr | None = None
    answered_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class MailQueueItem(BaseModel):
    mail_queue_id: str = Field(default_factory=new_id)
    contest_id: str | None = None
    mail_type: str
    recipient_email: EmailStr
    subject: str
    body_text: str
    body_html: str | None = None
    status: str = "pending"
    last_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class JudgeNode(BaseModel):
    judge_node_id: str = Field(default_factory=new_id)
    node_name: str
    total_slots: int = 10
    free_slots: int = 10
    running_job_count: int = 0
    agent_version: str = "unknown"
    last_heartbeat_at: datetime = Field(default_factory=now_utc)
    schedulable: bool = True
    allow_zoj: bool = True
    allow_editor: bool = True
    execution_capabilities: list[str] = Field(default_factory=list)
    execution_revision: str | None = None


class JudgeAgentLog(BaseModel):
    judge_agent_log_id: str = Field(default_factory=new_id)
    judge_node_id: str
    node_name: str
    level: str = "info"
    message: str
    created_at: datetime = Field(default_factory=now_utc)


class OperationalAuditLog(BaseModel):
    operational_audit_log_id: str = Field(default_factory=new_id)
    scope: str
    action: str
    method: str
    path: str
    status_code: int
    actor_email: EmailStr | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    contest_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class AccessLog(BaseModel):
    access_log_id: str = Field(default_factory=new_id)
    event_type: str
    account_scope: str
    email: EmailStr | None = None
    display_name: str | None = None
    contest_id: str | None = None
    contest_title: str | None = None
    participant_team_id: str | None = None
    team_name: str | None = None
    team_member_id: str | None = None
    member_name: str | None = None
    actor_role: str | None = None
    session_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


def demo_times() -> tuple[datetime, datetime, datetime]:
    start = now_utc() - timedelta(hours=1)
    end = now_utc() + timedelta(hours=3)
    freeze = end - timedelta(hours=1)
    return start, end, freeze


class ApiResponse(BaseModel):
    data: Any
    request_id: str = Field(default_factory=new_id)
