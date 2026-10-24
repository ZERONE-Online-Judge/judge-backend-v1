import json
import re
from functools import lru_cache
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria

from app.settings import settings


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
if settings.database_url.startswith("postgresql"):
    options = []
    if settings.database_statement_timeout_ms:
        options.append(f"-c statement_timeout={settings.database_statement_timeout_ms}")
    if settings.database_lock_timeout_ms:
        options.append(f"-c lock_timeout={settings.database_lock_timeout_ms}")
    if options:
        connect_args["options"] = " ".join(options)
pool_options = {} if settings.database_url.startswith("sqlite") else {
    "pool_size": settings.database_pool_size,
    "max_overflow": settings.database_max_overflow,
    "pool_timeout": settings.database_pool_timeout_seconds,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True, **pool_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@lru_cache(maxsize=1)
def _status_session_factory():
    status_engine = create_engine(settings.database_url, connect_args=connect_args,
        pool_size=1, max_overflow=0, pool_timeout=0.5, pool_pre_ping=True, pool_recycle=1800)
    return sessionmaker(bind=status_engine, autoflush=False, expire_on_commit=False)


def status_session():
    # Preserve single-connection in-memory SQLite installations/tests.
    return SessionLocal() if settings.database_url.startswith("sqlite") else _status_session_factory()()


class Base(DeclarativeBase):
    pass


@event.listens_for(Session, "do_orm_execute")
def exclude_deleted_contests(state):
    # Apply the tombstone to ORM queries and joins, including account profiles
    # and public metadata. Only internal history/maintenance queries opt out.
    if state.is_select and not state.execution_options.get("include_deleted_contests", False):
        from app.orm_models import ContestRow
        state.statement = state.statement.options(with_loader_criteria(
            ContestRow, lambda contest: contest.deleted_at.is_(None), include_aliases=True))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def backfill_assigned_contest_masters(connection) -> None:
    """Recover only administrator assignments with an explicit contest in the audit trail.

    Old contest-creation events did not record the new contest id, so they are not
    guessed here. Legacy wildcard accounts retain master access in all cases.
    """
    if "operational_audit_logs" not in inspect(connection).get_table_names():
        return
    logs = connection.execute(text("SELECT path, details FROM operational_audit_logs WHERE scope = 'admin' AND method = 'POST' AND status_code >= 200 AND status_code < 300"))
    assignments: dict[str, set[str]] = {}
    for path, details in logs:
        match = re.fullmatch(r"/api/admin/contests/([^/]+)/operators", path)
        if not match:
            continue
        body = json.loads(details or "{}").get("body", {})
        if isinstance(body, dict) and isinstance(body.get("email"), str):
            assignments.setdefault(body["email"].strip().lower(), set()).add(match.group(1))
    for row in connection.execute(text("SELECT staff_account_id, email, contest_scopes, contest_roles, protected_master_contests FROM staff_accounts")).mappings():
        scopes = json.loads(row["contest_scopes"] or "{}")
        roles = json.loads(row["contest_roles"] or "{}")
        protected = set(json.loads(row["protected_master_contests"] or "[]"))
        recovered = {cid for cid in assignments.get(row["email"].lower(), set()) if "contest.*" in scopes.get(cid, [])}
        if not recovered - protected:
            continue
        for cid in recovered:
            roles[cid] = ["master"]
        connection.execute(text("UPDATE staff_accounts SET contest_roles = :roles, protected_master_contests = :protected WHERE staff_account_id = :account_id"), {"roles": json.dumps(roles), "protected": json.dumps(sorted(protected | recovered)), "account_id": row["staff_account_id"]})


def backfill_separate_notice_roles(connection) -> None:
    """Make the existing combined board/notice grant explicit without losing access.

    Newly assigned board-only roles have no notice scope and are left unchanged.
    This makes the correction safe both as a one-time migration and on SQLite
    startup, where schema upgrades run without Alembic.
    """
    rows = connection.execute(text("SELECT staff_account_id, contest_scopes, contest_roles FROM staff_accounts")).mappings()
    for row in rows:
        scopes = json.loads(row["contest_scopes"] or "{}")
        roles = json.loads(row["contest_roles"] or "{}")
        changed = False
        for contest_id, selected in roles.items():
            if (
                "posts_manager" in selected
                and "notices_manager" not in selected
                and "master" not in selected
                and "contest.notice.manage" in scopes.get(contest_id, [])
            ):
                selected.append("notices_manager")
                changed = True
        if changed:
            connection.execute(text("UPDATE staff_accounts SET contest_roles = :roles WHERE staff_account_id = :account_id"), {"roles": json.dumps(roles), "account_id": row["staff_account_id"]})


def create_schema() -> None:
    from app import orm_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_submitted_id ON submissions (submitted_at DESC, submission_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_submitted_id ON submissions (contest_id, submitted_at DESC, submission_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_division_submitted_id ON submissions (contest_id, division_id, submitted_at DESC, submission_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_team_submitted_id ON submissions (contest_id, participant_team_id, submitted_at DESC, submission_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_problem_submitted_id ON submissions (contest_id, problem_id, submitted_at DESC, submission_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_judge_jobs_contest_status_queue ON judge_jobs (contest_id, status, queue_position)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_judge_jobs_submission_created ON judge_jobs (submission_id, created_at DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_judge_agent_logs_node_created ON judge_agent_logs (judge_node_id, created_at DESC, judge_agent_log_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_operational_audit_scope_created ON operational_audit_logs (scope, created_at DESC, operational_audit_log_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_operational_audit_contest_created ON operational_audit_logs (contest_id, created_at DESC, operational_audit_log_id DESC)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_operational_audit_actor_created ON operational_audit_logs (actor_email, created_at DESC, operational_audit_log_id DESC)"))
    if settings.database_url.startswith("sqlite"):
        inspector = inspect(engine)
        if "deleted_at" not in {column["name"] for column in inspector.get_columns("contests")}:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE contests ADD COLUMN deleted_at DATETIME"))
        if "owner_staff_account_id" not in {column["name"] for column in inspector.get_columns("contests")}:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE contests ADD COLUMN owner_staff_account_id VARCHAR(36) REFERENCES staff_accounts(staff_account_id)"))
        if "staff_accounts" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("staff_accounts")}
            with engine.begin() as connection:
                if "contest_roles" not in columns:
                    connection.execute(text("ALTER TABLE staff_accounts ADD COLUMN contest_roles TEXT NOT NULL DEFAULT '{}'"))
                if "protected_master_contests" not in columns:
                    connection.execute(text("ALTER TABLE staff_accounts ADD COLUMN protected_master_contests TEXT NOT NULL DEFAULT '[]'"))
                    backfill_assigned_contest_masters(connection)
                backfill_separate_notice_roles(connection)
                from app.services.contest_ownership import backfill_contest_owners
                backfill_contest_owners(connection)
        if "judge_jobs" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("judge_jobs")}
            if "leased_at" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE judge_jobs ADD COLUMN leased_at DATETIME"))
        if "submissions" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("submissions")}
            with engine.begin() as connection:
                if "judge_message" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN judge_message TEXT"))
                if "failed_testcase_order" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN failed_testcase_order INTEGER"))
                if "progress_current" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN progress_current INTEGER"))
                if "progress_total" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN progress_total INTEGER"))
                if "runtime_ms" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN runtime_ms INTEGER"))
                if "memory_kb" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN memory_kb INTEGER"))
                if "submission_kind" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN submission_kind VARCHAR(32) DEFAULT 'participant' NOT NULL"))
                if "submitted_by_name" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN submitted_by_name VARCHAR(120)"))
                if "submitted_by_email" not in columns:
                    connection.execute(text("ALTER TABLE submissions ADD COLUMN submitted_by_email VARCHAR(255)"))
            submission_columns = {
                column["name"]: column for column in inspect(engine).get_columns("submissions")
            }
            if (
                "participant_team_id" in submission_columns
                and not submission_columns["participant_team_id"].get("nullable", True)
            ) or (
                "team_member_id" in submission_columns
                and not submission_columns["team_member_id"].get("nullable", True)
            ):
                with engine.begin() as connection:
                    connection.execute(text("PRAGMA foreign_keys=OFF"))
                    connection.execute(text("ALTER TABLE submissions RENAME TO submissions_old"))
                    connection.execute(
                        text(
                            """
                            CREATE TABLE submissions (
                                submission_id VARCHAR(36) NOT NULL PRIMARY KEY,
                                contest_id VARCHAR(36) NOT NULL,
                                division_id VARCHAR(36) NOT NULL,
                                problem_id VARCHAR(36) NOT NULL,
                                participant_team_id VARCHAR(36),
                                team_member_id VARCHAR(36),
                                submission_kind VARCHAR(32) DEFAULT 'participant' NOT NULL,
                                submitted_by_name VARCHAR(120),
                                submitted_by_email VARCHAR(255),
                                language VARCHAR(32) NOT NULL,
                                source_code TEXT NOT NULL,
                                status VARCHAR(32) NOT NULL,
                                submitted_at DATETIME,
                                status_updated_at DATETIME,
                                compile_message TEXT,
                                judge_message TEXT,
                                failed_testcase_order INTEGER,
                                progress_current INTEGER,
                                progress_total INTEGER,
                                runtime_ms INTEGER,
                                memory_kb INTEGER,
                                FOREIGN KEY(contest_id) REFERENCES contests (contest_id),
                                FOREIGN KEY(division_id) REFERENCES contest_divisions (division_id),
                                FOREIGN KEY(problem_id) REFERENCES problems (problem_id),
                                FOREIGN KEY(participant_team_id) REFERENCES participant_teams (participant_team_id),
                                FOREIGN KEY(team_member_id) REFERENCES team_members (team_member_id)
                            )
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO submissions (
                                submission_id, contest_id, division_id, problem_id,
                                participant_team_id, team_member_id, submission_kind,
                                submitted_by_name, submitted_by_email, language,
                                source_code, status, submitted_at, status_updated_at,
                                compile_message, judge_message, failed_testcase_order,
                                progress_current, progress_total, runtime_ms, memory_kb
                            )
                            SELECT
                                submission_id, contest_id, division_id, problem_id,
                                participant_team_id, team_member_id, submission_kind,
                                submitted_by_name, submitted_by_email, language,
                                source_code, status, submitted_at, status_updated_at,
                                compile_message, judge_message, failed_testcase_order,
                                progress_current, progress_total, runtime_ms, memory_kb
                            FROM submissions_old
                            """
                        )
                    )
                    connection.execute(text("DROP TABLE submissions_old"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_contest_id ON submissions (contest_id)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_division_id ON submissions (division_id)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_problem_id ON submissions (problem_id)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_participant_team_id ON submissions (participant_team_id)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_team_member_id ON submissions (team_member_id)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_status ON submissions (status)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_submission_kind ON submissions (submission_kind)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_submitted_id ON submissions (submitted_at DESC, submission_id DESC)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_submitted_id ON submissions (contest_id, submitted_at DESC, submission_id DESC)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_division_submitted_id ON submissions (contest_id, division_id, submitted_at DESC, submission_id DESC)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_team_submitted_id ON submissions (contest_id, participant_team_id, submitted_at DESC, submission_id DESC)"))
                    connection.execute(text("CREATE INDEX IF NOT EXISTS idx_submissions_contest_problem_submitted_id ON submissions (contest_id, problem_id, submitted_at DESC, submission_id DESC)"))
                    connection.execute(text("PRAGMA foreign_keys=ON"))
        if "contests" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("contests")}
            with engine.begin() as connection:
                for visibility_column in ("visibility", "visibility_after_end"):
                    if visibility_column not in columns:
                        connection.execute(text(f"ALTER TABLE contests ADD COLUMN {visibility_column} VARCHAR(16) DEFAULT 'public' NOT NULL"))
                if "problem_access_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN problem_access_after_end VARCHAR(32) DEFAULT 'private' NOT NULL"))
                if "scoreboard_public_during_contest" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN scoreboard_public_during_contest BOOLEAN DEFAULT 0 NOT NULL"))
                if "scoreboard_access_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN scoreboard_access_after_end VARCHAR(32) DEFAULT 'private' NOT NULL"))
                if "submission_access_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN submission_access_after_end VARCHAR(32) DEFAULT 'private' NOT NULL"))
                for legacy_column in ("problem_public_after_end", "scoreboard_public_after_end", "submission_public_after_end"):
                    if legacy_column in columns:
                        connection.execute(text(f"ALTER TABLE contests DROP COLUMN {legacy_column}"))
                if "board_access_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN board_access_after_end VARCHAR(32) DEFAULT 'participants' NOT NULL"))
                if "board_write_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN board_write_after_end BOOLEAN DEFAULT 0 NOT NULL"))
                for field, default in (
                    ("board_question_visibility_policy", "choice"),
                    ("board_question_default_visibility", "public"),
                    ("board_comment_scope_policy", "choice"),
                    ("board_comment_default_scope", "all"),
                ):
                    if field not in columns:
                        connection.execute(text(f"ALTER TABLE contests ADD COLUMN {field} VARCHAR(16) DEFAULT '{default}' NOT NULL"))
                if "notice_access_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN notice_access_after_end VARCHAR(32) DEFAULT 'public' NOT NULL"))
                if "editorial_access_after_end" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN editorial_access_after_end VARCHAR(32) DEFAULT 'private' NOT NULL"))
                if "scoreboard_freeze_mode" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN scoreboard_freeze_mode VARCHAR(32) DEFAULT 'auto' NOT NULL"))
                if "scoreboard_release_mode" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN scoreboard_release_mode VARCHAR(32) DEFAULT 'manual' NOT NULL"))
                if "scoreboard_frozen_at" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN scoreboard_frozen_at DATETIME"))
                    connection.execute(text("UPDATE contests SET scoreboard_frozen_at = CURRENT_TIMESTAMP WHERE scoreboard_freeze_mode = 'frozen'"))
                if "mock_judging_enabled" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN mock_judging_enabled BOOLEAN DEFAULT 0 NOT NULL"))
                if "freeze_submission_count_visible" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN freeze_submission_count_visible BOOLEAN DEFAULT 0 NOT NULL"))
                if "participant_progress_visible" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN participant_progress_visible BOOLEAN DEFAULT 0 NOT NULL"))
                if "mock_judging_progress_visible" not in columns:
                    connection.execute(text("ALTER TABLE contests ADD COLUMN mock_judging_progress_visible BOOLEAN DEFAULT 0 NOT NULL"))
        if "contest_questions" in inspector.get_table_names():
            if "comment_scope" not in {column["name"] for column in inspector.get_columns("contest_questions")}:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE contest_questions ADD COLUMN comment_scope VARCHAR(16) DEFAULT 'all' NOT NULL"))
        if "scoreboard_releases" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("scoreboard_releases")}
            with engine.begin() as connection:
                if "strategy" not in columns:
                    connection.execute(text("ALTER TABLE scoreboard_releases ADD COLUMN strategy VARCHAR(32) DEFAULT 'manual' NOT NULL"))
                if "resolver_state" not in columns:
                    connection.execute(text("ALTER TABLE scoreboard_releases ADD COLUMN resolver_state JSON"))
                if "undo_history" not in columns:
                    connection.execute(text("ALTER TABLE scoreboard_releases ADD COLUMN undo_history JSON"))
                if "revision" not in columns:
                    connection.execute(text("ALTER TABLE scoreboard_releases ADD COLUMN revision INTEGER DEFAULT 0 NOT NULL"))
        if "mail_queue" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("mail_queue")}
            if "body_html" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE mail_queue ADD COLUMN body_html TEXT"))
            with engine.begin() as connection:
                for name, kind in (("contest_id", "VARCHAR(36)"), ("last_attempt_at", "DATETIME"), ("sent_at", "DATETIME")):
                    if name not in columns:
                        connection.execute(text(f"ALTER TABLE mail_queue ADD COLUMN {name} {kind}"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS idx_mail_queue_created ON mail_queue (created_at, mail_queue_id)"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS idx_mail_queue_contest_created ON mail_queue (contest_id, created_at, mail_queue_id)"))
                if "contest_id" not in columns:
                    from app.services.mail_log_backfill import backfill_mail_contests
                    backfill_mail_contests(connection)
        if "team_members" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("team_members")}
            if "session_revoked_at" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE team_members ADD COLUMN session_revoked_at DATETIME"))
        if "judge_nodes" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("judge_nodes")}
            for name, declaration in [("allow_zoj", "BOOLEAN NOT NULL DEFAULT 1"), ("allow_editor", "BOOLEAN NOT NULL DEFAULT 1"), ("execution_capabilities", "JSON NOT NULL DEFAULT '[]'"), ("execution_revision", "VARCHAR(128)")]:
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE judge_nodes ADD COLUMN {name} {declaration}"))
            if "agent_version" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE judge_nodes ADD COLUMN agent_version VARCHAR(64) DEFAULT 'unknown' NOT NULL"))
        if "staff_accounts" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("staff_accounts")}
            if "password_hash" in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE staff_accounts DROP COLUMN password_hash"))
        if "problem_assets" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("problem_assets")}
            if "source_language" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE problem_assets ADD COLUMN source_language VARCHAR(32)"))
        if "problems" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("problems")}
            if "allowed_languages" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("""ALTER TABLE problems ADD COLUMN allowed_languages JSON DEFAULT '["cpp17","python311","java8","c99","pypy3","kotlin"]' NOT NULL"""))
            if "language_resource_limits" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE problems ADD COLUMN language_resource_limits JSON DEFAULT '{}' NOT NULL"))
            if "editorial" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE problems ADD COLUMN editorial TEXT DEFAULT '' NOT NULL"))
            for name, kind in (("default_statement_language", "VARCHAR(100) DEFAULT 'ko'"),
                               ("statement_translations", "JSON DEFAULT '{}'"), ("editorial_translations", "JSON DEFAULT '{}'")):
                if name not in columns:
                    with engine.begin() as connection:
                        connection.execute(text(f"ALTER TABLE problems ADD COLUMN {name} {kind} NOT NULL"))
            if "max_score" in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE problems DROP COLUMN max_score"))
        if "submissions" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("submissions")}
            if "awarded_score" in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE submissions DROP COLUMN awarded_score"))
