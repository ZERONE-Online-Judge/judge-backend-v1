from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models import new_id, now_utc


class ContestRow(Base):
    __tablename__ = "contests"

    contest_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_staff_account_id: Mapped[str | None] = mapped_column(ForeignKey("staff_accounts.staff_account_id", name="fk_contest_owner"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    organization_name: Mapped[str] = mapped_column(String(255))
    overview: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="public", server_default="public")
    visibility_after_end: Mapped[str] = mapped_column(String(16), default="public", server_default="public")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    freeze_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    problem_access_after_end: Mapped[str] = mapped_column(String(32), default="private")
    scoreboard_access_after_end: Mapped[str] = mapped_column(String(32), default="private")
    submission_access_after_end: Mapped[str] = mapped_column(String(32), default="private")
    board_access_after_end: Mapped[str] = mapped_column(String(32), default="participants")
    board_write_after_end: Mapped[bool] = mapped_column(Boolean, default=False)
    notice_access_after_end: Mapped[str] = mapped_column(String(32), default="public")
    editorial_access_after_end: Mapped[str] = mapped_column(String(32), default="private")
    scoreboard_freeze_mode: Mapped[str] = mapped_column(String(32), default="auto")
    scoreboard_release_mode: Mapped[str] = mapped_column(String(32), default="manual")
    scoreboard_frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mock_judging_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    participant_progress_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    mock_judging_progress_visible: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    divisions: Mapped[list["ContestDivisionRow"]] = relationship(back_populates="contest")


class ContestDivisionRow(Base):
    __tablename__ = "contest_divisions"
    __table_args__ = (UniqueConstraint("contest_id", "code", name="uq_contest_division_code"),)

    division_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    display_order: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    contest: Mapped[ContestRow] = relationship(back_populates="divisions")


class ScoreboardReleaseRow(Base):
    __tablename__ = "scoreboard_releases"

    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id"), primary_key=True)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="partial")
    strategy: Mapped[str] = mapped_column(String(32), default="manual")
    resolver_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_rows: Mapped[list] = mapped_column(JSON, default=list)
    revealed_ranks: Mapped[list] = mapped_column(JSON, default=list)
    undo_history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TeamMemberRow(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("contest_id", "email", name="uq_team_member_contest_email"),
        Index("ix_team_members_email_lower", text("lower(email)")),
    )

    team_member_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    participant_team_id: Mapped[str] = mapped_column(ForeignKey("participant_teams.participant_team_id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    active_sessions: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ParticipantTeamRow(Base):
    __tablename__ = "participant_teams"
    __table_args__ = (UniqueConstraint("contest_id", "team_name", name="uq_participant_team_contest_name"),)

    participant_team_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id"), index=True)
    team_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="invited")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    members: Mapped[list[TeamMemberRow]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ProblemRow(Base):
    __tablename__ = "problems"
    __table_args__ = (UniqueConstraint("contest_id", "division_id", "problem_code", name="uq_problem_division_code"),)

    problem_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id"), index=True)
    problem_code: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    statement: Mapped[str] = mapped_column(Text)
    editorial: Mapped[str] = mapped_column(Text, default="")
    default_statement_language: Mapped[str] = mapped_column(String(100), default="ko", server_default="ko")
    statement_translations: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    editorial_translations: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    time_limit_ms: Mapped[int] = mapped_column(Integer)
    memory_limit_mb: Mapped[int] = mapped_column(Integer)
    allowed_languages: Mapped[list] = mapped_column(JSON, default=lambda: ["cpp17", "python313", "java8", "c99", "pypy3", "kotlin"])
    language_resource_limits: Mapped[dict] = mapped_column(JSON, default=dict)
    display_order: Mapped[int] = mapped_column(Integer)


class ProblemAssetRow(Base):
    __tablename__ = "problem_assets"

    asset_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.problem_id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(120))
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    source_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asset_status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TestcaseSetRow(Base):
    __tablename__ = "testcase_sets"

    testcase_set_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.problem_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TestcaseRow(Base):
    __tablename__ = "testcases"

    testcase_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    testcase_set_id: Mapped[str] = mapped_column(ForeignKey("testcase_sets.testcase_set_id"), index=True)
    display_order: Mapped[int] = mapped_column(Integer)
    input_storage_key: Mapped[str] = mapped_column(String(512))
    output_storage_key: Mapped[str] = mapped_column(String(512))
    input_sha256: Mapped[str] = mapped_column(String(64))
    output_sha256: Mapped[str] = mapped_column(String(64))
    time_limit_ms_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_limit_mb_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class SubmissionRow(Base):
    __tablename__ = "submissions"

    submission_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id"), index=True)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.problem_id"), index=True)
    participant_team_id: Mapped[str | None] = mapped_column(ForeignKey("participant_teams.participant_team_id"), nullable=True, index=True)
    team_member_id: Mapped[str | None] = mapped_column(ForeignKey("team_members.team_member_id"), nullable=True, index=True)
    submission_kind: Mapped[str] = mapped_column(String(32), default="participant", index=True)
    submitted_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    submitted_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(32))
    source_code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    status_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    compile_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    judge_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_testcase_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)


class JudgeJobRow(Base):
    __tablename__ = "judge_jobs"

    judge_job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.submission_id"), index=True)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    queue_position: Mapped[int] = mapped_column(Integer, index=True)
    assigned_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class RejudgingRow(Base):
    __tablename__ = "rejudgings"

    rejudging_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.problem_id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict] = mapped_column(JSON)
    judge_context: Mapped[dict] = mapped_column(JSON)
    context_hash: Mapped[str] = mapped_column(String(64))
    requested_by_name: Mapped[str] = mapped_column(String(120))
    requested_by_email: Mapped[str] = mapped_column(String(255))
    decided_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RejudgingItemRow(Base):
    __tablename__ = "rejudging_items"
    __table_args__ = (Index("ix_rejudging_items_batch_submission", "rejudging_id", "submission_id"),)

    rejudging_item_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rejudging_id: Mapped[str] = mapped_column(ForeignKey("rejudgings.rejudging_id"), index=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.submission_id"))
    judge_job_id: Mapped[str] = mapped_column(ForeignKey("judge_jobs.judge_job_id"), unique=True)
    # A nullable unique key prevents overlapping sessions, including ready ones.
    active_submission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    original_result: Mapped[dict] = mapped_column(JSON)
    candidate_result: Mapped[dict] = mapped_column(JSON)
    verification_context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class NotificationRow(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("rejudging_item_id", "team_member_id", name="uq_rejudge_notice_recipient"),
        Index("ix_notification_feed", "team_member_id", "deleted_at", "created_at"),
        Index("ix_notification_contest", "contest_id", "notification_id"),
        UniqueConstraint("event_key", "team_member_id", name="uq_notification_event_recipient"),
    )

    notification_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rejudging_item_id: Mapped[str | None] = mapped_column(ForeignKey("rejudging_items.rejudging_item_id", ondelete="CASCADE"))
    team_member_id: Mapped[str] = mapped_column(ForeignKey("team_members.team_member_id", ondelete="CASCADE"))
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id", ondelete="CASCADE"))
    submission_id: Mapped[str | None] = mapped_column(ForeignKey("submissions.submission_id", ondelete="CASCADE"))
    problem_id: Mapped[str | None] = mapped_column(ForeignKey("problems.problem_id", ondelete="CASCADE"))
    contest_title: Mapped[str] = mapped_column(String(255))
    problem_code: Mapped[str | None] = mapped_column(String(16))
    old_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    notification_type: Mapped[str] = mapped_column(String(32), default="rejudging", server_default="rejudging")
    event_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    href: Mapped[str | None] = mapped_column(Text, nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Keep the old import available for the legacy rejudging API.
RejudgingNotificationRow = NotificationRow

class ServiceNoticeRow(Base):
    __tablename__ = "service_notices"

    service_notice_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ContestNoticeRow(Base):
    __tablename__ = "contest_notices"

    contest_notice_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility: Mapped[str] = mapped_column(String(32), default="public", index=True)
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ContestQuestionRow(Base):
    __tablename__ = "contest_questions"

    contest_question_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    participant_team_id: Mapped[str] = mapped_column(ForeignKey("participant_teams.participant_team_id"), index=True)
    team_member_id: Mapped[str] = mapped_column(ForeignKey("team_members.team_member_id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(32), default="public", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    answers: Mapped[list["ContestQuestionAnswerRow"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ContestQuestionAnswerRow(Base):
    __tablename__ = "contest_question_answers"

    contest_answer_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_question_id: Mapped[str] = mapped_column(ForeignKey("contest_questions.contest_question_id"), index=True)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(32), default="public", index=True)
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ContactInquiryRow(Base):
    __tablename__ = "contact_inquiries"

    contact_inquiry_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    sender_name: Mapped[str] = mapped_column(String(120))
    sender_email: Mapped[str] = mapped_column(String(255), index=True)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    answer_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class StaffAccountRow(Base):
    __tablename__ = "staff_accounts"

    staff_account_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    is_service_master: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[str] = mapped_column(Text, default="")
    contest_scopes: Mapped[str] = mapped_column(Text, default="{}")
    contest_roles: Mapped[str] = mapped_column(Text, default="{}")
    protected_master_contests: Mapped[str] = mapped_column(Text, default="[]")


class EditorNonceRow(Base):
    __tablename__ = "editor_bridge_nonces"
    nonce: Mapped[str] = mapped_column(String(32), primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer, index=True)


class AuthRateLimitRow(Base):
    __tablename__ = "auth_rate_limits"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[int] = mapped_column(Integer, index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)


class AccountPasswordRow(Base):
    __tablename__ = "account_passwords"
    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[int] = mapped_column(Integer)


class IdentityAccountRow(Base):
    __tablename__ = "identity_accounts"
    account_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PasswordChallengeRow(Base):
    __tablename__ = "password_challenges"
    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), primary_key=True)
    code_digest: Mapped[str] = mapped_column(String(64))
    expected_password_hash: Mapped[str] = mapped_column(String(255))
    requested_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class EditorImportRow(Base):
    __tablename__ = "editor_problem_imports"
    operation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(255))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.problem_id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class StaffSessionRow(Base):
    __tablename__ = "staff_sessions"

    staff_session_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    staff_account_id: Mapped[str] = mapped_column(ForeignKey("staff_accounts.staff_account_id"), index=True)
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GeneralSessionRow(Base):
    __tablename__ = "general_sessions"

    general_session_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), index=True)
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ParticipantPreviewSessionRow(Base):
    __tablename__ = "participant_preview_sessions"

    general_session_id: Mapped[str] = mapped_column(ForeignKey("general_sessions.general_session_id", ondelete="CASCADE"), primary_key=True)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id", ondelete="CASCADE"), primary_key=True)
    staff_account_id: Mapped[str] = mapped_column(ForeignKey("staff_accounts.staff_account_id", ondelete="CASCADE"), index=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id", ondelete="CASCADE"))


class ParticipantPreviewQuestionRow(Base):
    __tablename__ = "participant_preview_questions"

    staff_account_id: Mapped[str] = mapped_column(ForeignKey("staff_accounts.staff_account_id", ondelete="CASCADE"), primary_key=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id", ondelete="CASCADE"), index=True)
    source_question_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[str] = mapped_column(Text)


class MailQueueItemRow(Base):
    __tablename__ = "mail_queue"

    mail_queue_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mail_type: Mapped[str] = mapped_column(String(64))
    recipient_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class BundleWarmQueueItemRow(Base):
    __tablename__ = "bundle_warm_queue"

    bundle_warm_queue_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JudgeNodeRow(Base):
    __tablename__ = "judge_nodes"

    judge_node_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_name: Mapped[str] = mapped_column(String(120), unique=True)
    node_secret_hash: Mapped[str] = mapped_column(Text)
    total_slots: Mapped[int] = mapped_column(Integer, default=10)
    free_slots: Mapped[int] = mapped_column(Integer, default=10)
    running_job_count: Mapped[int] = mapped_column(Integer, default=0)
    agent_version: Mapped[str] = mapped_column(String(64), default="unknown")
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    schedulable: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_zoj: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    allow_editor: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    execution_capabilities: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    execution_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)


class JudgeAgentLogRow(Base):
    __tablename__ = "judge_agent_logs"
    __table_args__ = (Index("idx_judge_logs_created_id", "created_at", "judge_agent_log_id"),)

    judge_agent_log_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    judge_node_id: Mapped[str] = mapped_column(String(36), index=True)
    node_name: Mapped[str] = mapped_column(String(120), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


class OperationalAuditLogRow(Base):
    __tablename__ = "operational_audit_logs"
    __table_args__ = (Index("idx_audit_logs_created_id", "created_at", "operational_audit_log_id"),)

    operational_audit_log_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(12), index=True)
    path: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer, index=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    actor_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    contest_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


class AccessLogRow(Base):
    __tablename__ = "access_logs"
    __table_args__ = (Index("idx_access_logs_created_id", "created_at", "access_log_id"),)

    access_log_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    account_scope: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contest_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    contest_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    participant_team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    team_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    team_member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    member_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


class UsageEventRow(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("idx_usage_created", "created_at"),
        Index("idx_usage_contest_created", "contest_id", "created_at"),
        Index("idx_usage_visitor_created", "visitor_key", "created_at"),
        Index("idx_usage_seen", "last_seen_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    visitor_key: Mapped[str] = mapped_column(String(64))
    visit_key: Mapped[str] = mapped_column(String(64))
    account_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service: Mapped[str] = mapped_column(String(24))
    page_key: Mapped[str] = mapped_column(String(64))
    audience: Mapped[str] = mapped_column(String(24))
    device: Mapped[str] = mapped_column(String(16))
    browser: Mapped[str] = mapped_column(String(24))
    referrer_host: Mapped[str] = mapped_column(String(253), default="direct")
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class OtpCodeRow(Base):
    __tablename__ = "otp_codes"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    code: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TeamSessionRow(Base):
    __tablename__ = "team_sessions"

    team_session_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id"), index=True)
    division_id: Mapped[str] = mapped_column(ForeignKey("contest_divisions.division_id"), index=True)
    participant_team_id: Mapped[str] = mapped_column(ForeignKey("participant_teams.participant_team_id"), index=True)
    team_member_id: Mapped[str] = mapped_column(ForeignKey("team_members.team_member_id"), index=True)
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PresentationAccountRow(Base):
    """A revocable display credential, never a staff or participant identity."""
    __tablename__ = "presentation_accounts"

    contest_id: Mapped[str] = mapped_column(ForeignKey("contests.contest_id", ondelete="CASCADE"), primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(36), default=new_id)
    email: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PresentationLoginLimitRow(Base):
    __tablename__ = "presentation_login_limits"

    client_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer)


class VerificationSnapshotRow(Base):
    __tablename__ = "verification_snapshots"
    context_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    context: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class VerificationRunRow(Base):
    __tablename__ = "verification_runs"
    submission_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_id: Mapped[str] = mapped_column(String(36), index=True)
    expected_status: Mapped[str] = mapped_column(String(32))
    context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (Index("idx_verification_run_asset_created", "problem_id", "asset_id", "created_at"),)


class VerificationExecutionRow(Base):
    __tablename__ = "verification_executions"
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    submission_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    testcases: Mapped[list] = mapped_column(JSON, default=list)
    results: Mapped[list] = mapped_column(JSON, default=list)
    storage_keys: Mapped[list] = mapped_column(JSON, default=list)
    testcase_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    full_suite_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    node_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)


class VerificationAnalysisRow(Base):
    __tablename__ = "verification_analyses"
    analysis_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    model: Mapped[str] = mapped_column(String(100))
    context_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    coverage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    engine_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    agent_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class VerificationTrialRow(Base):
    __tablename__ = "verification_trials"
    submission_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(36), index=True)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    context_hash: Mapped[str] = mapped_column(String(64))
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    artifact_id: Mapped[str] = mapped_column(String(100))
    testcase_orders: Mapped[list | None] = mapped_column(JSON, nullable=True)
    testcase_count: Mapped[int] = mapped_column(Integer)
    probe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class VerificationTaskRow(Base):
    __tablename__ = "verification_tasks"
    analysis_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contest_id: Mapped[str] = mapped_column(String(36), index=True)
    problem_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    goal: Mapped[str] = mapped_column(Text)
    source_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class EditorJudgeRunRow(Base):
    """Standalone Editor work; never a contest submission or scoreboard entry."""
    __tablename__ = "editor_judge_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_key: Mapped[str] = mapped_column(String(64), unique=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    dataset_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    project_id: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    storage_keys: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    testcase_count: Mapped[int] = mapped_column(Integer)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    assigned_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    node_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EditorExecutionTaskRow(Base):
    """Bounded tool invocations; separate from submissions and scoreboards."""
    __tablename__ = "editor_execution_tasks"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_key: Mapped[str] = mapped_column(String(64), unique=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    input_key: Mapped[str] = mapped_column(Text)
    result_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    assigned_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    node_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
