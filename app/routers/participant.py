from app.judge_languages import LANGUAGES, require_allowed
from app.services.request_sessions import request_database
import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel, EmailStr, Field
from starlette.concurrency import run_in_threadpool
from app.services.submission_status import PENDING, wait_for_submission, read_status_snapshot
from app.database import status_session
from app.services.scoreboard_response import scoreboard_response

from app.models import ContestResourceAccess, ContestStatus, SubmissionStatus, now_utc
from app.services.access_logging import write_access_log
from app.services.authz import bearer_token, has_contest_permission, require_participant
from app.services.contest_visibility import contest_has_ended, contest_payload_for_view
from app.services.errors import AppError, authentication_required, invalid_state, not_found, permission_denied
from app.services.mail_templates import absolute_url, render_branded_email
from app.services.responses import ok, page
from app.services.participant_preview import (
    create_preview_answer, create_preview_question, create_preview_submission,
    list_preview_submissions, owns_submission, preview_questions, preview_solve_statuses,
)
from app.services.store import OPERATOR_TEST_TEAM_PREFIX, SessionConflictError, store
from app.services.storage import object_storage
from app.services.package_builder import is_editor_source_asset
from app.settings import settings

router = APIRouter(tags=["participant"])


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


def _sort_problems(items: list):
    return sorted(items, key=lambda item: (item.display_order, item.problem_code, item.title, item.problem_id))


def _problem_solve_statuses(contest_id: str, participant: dict | None) -> dict[str, str]:
    if not participant:
        return {}
    if participant.get("is_preview"):
        return preview_solve_statuses(participant)
    return store.participant_solve_statuses(contest_id, participant["team"].participant_team_id)


def _problem_payload(problem, solve_statuses: dict[str, str], include_editorial: bool = False) -> dict:
    item = problem.model_dump(mode="json")
    if not include_editorial:
        item["editorial"] = ""
        item["editorial_translations"] = {}
    item["solve_status"] = solve_statuses.get(problem.problem_id, "unsolved")
    return item


class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerifyRequest(BaseModel):
    email: EmailStr
    otp_code: str = ""
    force_new_session: bool = False


class SubmissionCreateRequest(BaseModel):
    language: str
    source_code: str = Field(min_length=1, max_length=524288)


class QuestionCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    visibility: str = "public"


class QuestionAnswerCreateRequest(BaseModel):
    body: str = Field(min_length=1)


def _is_ended(contest) -> bool:
    return contest_has_ended(contest)


def _has_started(contest) -> bool:
    return now_utc() >= contest.start_at


def _optional_participant(request: Request, contest_id: str) -> dict | None:
    token = bearer_token(request)
    if not token:
        return None
    return (
        store.get_participant_by_access_token(contest_id, token)
        or store.get_participant_by_general_access_token(contest_id, token)
    )


def _require_contest_participant(request: Request, contest_id: str) -> dict:
    participant = _optional_participant(request, contest_id)
    if not participant:
        raise authentication_required("Participant access token is required.")
    return participant


def _allow_after_end_resource(contest, access: ContestResourceAccess, participant: dict | None) -> bool:
    if access == ContestResourceAccess.PUBLIC:
        return True
    if access == ContestResourceAccess.PARTICIPANTS:
        return participant is not None
    return False


def _allow_editorial_view(contest, participant: dict | None) -> bool:
    if participant and participant.get("is_preview"):
        return False
    if not _is_ended(contest):
        return False
    if contest.problem_access_after_end == ContestResourceAccess.PRIVATE:
        return False
    return _allow_after_end_resource(contest, contest.editorial_access_after_end, participant)


def _allow_board_write_after_end(contest, participant: dict | None) -> bool:
    if not contest.board_write_after_end:
        return False
    return _allow_after_end_resource(contest, contest.board_access_after_end, participant)


def _allow_started_resource(contest, access: ContestResourceAccess, participant: dict | None) -> bool:
    if not _has_started(contest):
        return False
    if access == ContestResourceAccess.PUBLIC:
        return True
    return participant is not None


def _allow_visible_resource(access: ContestResourceAccess, participant: dict | None) -> bool:
    if access == ContestResourceAccess.PUBLIC:
        return True
    if access == ContestResourceAccess.PARTICIPANTS:
        return participant is not None
    return participant is not None


def _allow_problem_view(request: Request, contest_id: str, division_id: str | None = None) -> tuple[dict | None, object]:
    participant = _optional_participant(request, contest_id)
    contest = store.get_contest(contest_id) if participant and participant.get("is_preview") else store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    if participant and participant.get("is_preview"):
        if division_id and participant["division"].division_id != division_id:
            raise not_found("Division is not available for this participant.")
        return participant, contest
    if _is_ended(contest):
        if _allow_after_end_resource(contest, contest.problem_access_after_end, participant):
            return participant, contest
        raise not_found()
    if participant and division_id and participant["division"].division_id != division_id:
        raise not_found("Division is not available for this participant.")
    if _allow_started_resource(contest, contest.problem_access_after_end, participant):
        return participant, contest
    raise not_found()


def _allow_scoreboard_view(request: Request, contest_id: str, division_id: str | None = None) -> tuple[dict | None, object]:
    participant = _optional_participant(request, contest_id)
    contest = store.get_contest(contest_id) if participant and participant.get("is_preview") else store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    if participant and participant.get("is_preview"):
        if division_id and participant["division"].division_id != division_id:
            raise not_found("Division is not available for this participant.")
        return participant, contest
    if _is_ended(contest):
        if _allow_after_end_resource(contest, contest.scoreboard_access_after_end, participant):
            return participant, contest
        raise not_found()
    if participant and division_id and participant["division"].division_id != division_id:
        raise not_found("Division is not available for this participant.")
    if _allow_started_resource(contest, contest.scoreboard_access_after_end, participant):
        return participant, contest
    raise not_found()


def _allow_submission_list_view(request: Request, contest_id: str) -> tuple[dict | None, object]:
    participant = _optional_participant(request, contest_id)
    contest = store.get_contest(contest_id) if participant and participant.get("is_preview") else store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    if participant and participant.get("is_preview"):
        return participant, contest
    if _is_ended(contest):
        if _allow_after_end_resource(contest, contest.submission_access_after_end, participant):
            return participant, contest
        raise not_found()
    if _allow_started_resource(contest, contest.submission_access_after_end, participant):
        return participant, contest
    raise not_found()


def _participant_submission_payload(
    submission,
    include_source: bool,
    queue_position: int | None = None,
    source_code_length: int | None = None,
    show_progress: bool = True,
    team_name: str | None = None,
    member_name: str | None = None,
) -> dict:
    item = submission.model_dump(mode="json")
    item["team_name"] = team_name
    item["member_name"] = member_name
    item["queue_position"] = queue_position if show_progress else None
    if show_progress and submission.status == "waiting" and item["queue_position"] is None:
        item["queue_position"] = store.pending_queue_ranks(submission.contest_id,
            submission_ids=[submission.submission_id]).get(submission.submission_id)
    source_code = item.get("source_code") or ""
    item["source_code_length"] = source_code_length if source_code_length is not None else len(source_code.encode("utf-8"))
    progress_current = item.get("progress_current")
    progress_total = item.get("progress_total")
    item["progress_percent"] = None
    if show_progress and isinstance(progress_current, int) and isinstance(progress_total, int) and progress_total > 0:
        item["progress_percent"] = max(0, min(100, round((progress_current / progress_total) * 100)))
    item["progress_current"] = None
    item["progress_total"] = None
    # Participants should not receive internal judge diagnostics/logs.
    item["compile_message"] = None
    item["judge_message"] = None
    if not include_source:
        item["source_code"] = ""
    return item


def _is_mock_submission(submission) -> bool:
    kind = getattr(submission, "submission_kind", "participant")
    if kind == "mock_judging":
        return True
    if kind == "operator_test":
        return False
    team = store.teams.get(submission.participant_team_id)
    return bool(team and team.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX))


def _mock_submission_payload(submission, show_progress: bool = True) -> dict:
    item = _participant_submission_payload(submission, include_source=False, show_progress=show_progress)
    item["source_code"] = ""
    return item


def _participant_submission_payloads(
    submissions: list,
    *,
    include_source: bool,
    queue_ranks: dict[str, int],
    source_lengths: dict[str, int],
    show_progress: bool,
) -> list[dict]:
    teams = store.teams_by_ids([submission.participant_team_id for submission in submissions])
    members = {
        member.team_member_id: member
        for team in teams.values()
        for member in team.members
    }
    items = []
    for submission in submissions:
        team = teams.get(submission.participant_team_id)
        member = members.get(submission.team_member_id)
        items.append(
            _participant_submission_payload(
                submission,
                include_source=include_source,
                queue_position=queue_ranks.get(submission.submission_id),
                source_code_length=source_lengths.get(submission.submission_id),
                show_progress=show_progress,
                team_name=team.team_name if team else None,
                member_name=member.name if member else None,
            )
        )
    return items


def _submission_progress_visible(contest) -> bool:
    return bool(contest.participant_progress_visible)


@router.post("/contests/{contest_id}/participant-login/otp/request")
def request_otp(contest_id: str, payload: OtpRequest, request: Request):
    contest = store.get_public_contest(contest_id, allow_private=True)
    if not contest:
        raise not_found()
    team = store.get_team_by_email(contest_id, str(payload.email))
    if not team:
        raise not_found("Participant email is not registered.")
    retry_after = store.participant_otp_retry_after_seconds(contest_id, str(payload.email))
    if retry_after > 0:
        raise AppError(
            429,
            "otp_request_rate_limited",
            f"Please wait {retry_after} seconds before requesting another verification code.",
            {"retry_after_seconds": retry_after},
        )
    code = store.create_otp(contest_id, str(payload.email))
    data = {"sent": True, "delivery": "email", "cooldown_seconds": settings.otp_request_cooldown_seconds}
    if settings.allow_empty_otp:
        data["demo_otp"] = code
    return ok(request, data)


@router.post("/contests/{contest_id}/participant-login/otp/verify")
def verify_otp(contest_id: str, payload: OtpVerifyRequest, request: Request):
    try:
        verified = store.verify_otp(
            contest_id,
            str(payload.email),
            payload.otp_code,
            payload.force_new_session,
        )
    except SessionConflictError as exc:
        write_access_log(
            request,
            event_type="session_conflict",
            account_scope="participant",
            email=str(payload.email),
            contest_id=contest_id,
            details=exc.details,
        )
        raise AppError(
            409,
            "session_conflict",
            "이미 다른 브라우저 또는 기기에서 로그인 중입니다.",
            exc.details,
        )
    if not verified:
        write_access_log(
            request,
            event_type="login_failed",
            account_scope="participant",
            email=str(payload.email),
            contest_id=contest_id,
        )
        raise AppError(401, "invalid_credentials", "Invalid OTP.")
    team, member, division, access_token = verified
    write_access_log(
        request,
        event_type="participant_login",
        account_scope="participant",
        email=str(payload.email),
        display_name=member.name,
        contest_id=contest_id,
        participant_team_id=team.participant_team_id,
        team_name=team.team_name,
        team_member_id=member.team_member_id,
        member_name=member.name,
        actor_role="participant",
        details={"force_new_session": payload.force_new_session},
    )
    return ok(
        request,
        {
            "access_token": access_token,
            "team": team.model_dump(mode="json"),
            "member": member.model_dump(mode="json"),
            "division": division.model_dump(mode="json"),
            "workspace_path": f"/contests/{contest_id}/divisions/{team.division_id}/workspace",
        },
    )


@router.get("/contests/{contest_id}/participant-session/me")
@request_database
def participant_me(contest_id: str, request: Request):
    token = bearer_token(request)
    session = store.get_participant_by_access_token(contest_id, token) if token else None
    if not session:
        raise AppError(401, "authentication_required", "Participant access token is required.")
    if session.get("is_preview"):
        return ok(request, {"team": session["team"].model_dump(mode="json"), "member": session["member"].model_dump(mode="json"), "division": session["division"].model_dump(mode="json"), "is_preview": True})
    write_access_log(
        request,
        event_type="participant_session_check",
        account_scope="participant",
        email=session["member"].email,
        display_name=session["member"].name,
        contest_id=contest_id,
        participant_team_id=session["team"].participant_team_id,
        team_name=session["team"].team_name,
        team_member_id=session["member"].team_member_id,
        member_name=session["member"].name,
        actor_role="participant",
    )
    return ok(
        request,
        {
            "team": session["team"].model_dump(mode="json"),
            "member": session["member"].model_dump(mode="json"),
            "division": session["division"].model_dump(mode="json"),
        },
    )


@router.get("/contests/{contest_id}/workspace")
@request_database
def workspace(contest_id: str, request: Request, team_member_email: str | None = None):
    participant, contest = _allow_problem_view(request, contest_id)
    divisions = store.contest_divisions(contest_id)
    division = participant["division"] if participant else (divisions[0] if divisions else None)
    if not division:
        raise not_found("Contest division is not configured.")
    problems = _sort_problems(store.contest_problems(contest_id, division.division_id))
    solve_statuses = _problem_solve_statuses(contest_id, participant)
    contest_payload = contest_payload_for_view(contest, participant)
    return ok(
        request,
        {
            "contest": contest_payload,
            "division": division.model_dump(mode="json"),
            "divisions": [item.model_dump(mode="json") for item in divisions],
            "problems": [_problem_payload(p, solve_statuses) for p in problems],
            "emergency_notice": contest_payload["emergency_notice"],
            "is_preview": bool(participant and participant.get("is_preview")),
        },
    )


@router.get("/contests/{contest_id}/divisions/{division_id}/workspace")
@request_database
def division_workspace(contest_id: str, division_id: str, request: Request):
    participant, contest = _allow_problem_view(request, contest_id, division_id)
    division = store.get_division(contest_id, division_id)
    if not division:
        raise not_found()
    problems = _sort_problems(store.contest_problems(contest_id, division_id))
    solve_statuses = _problem_solve_statuses(contest_id, participant)
    contest_payload = contest_payload_for_view(contest, participant)
    return ok(
        request,
        {
            "contest": contest_payload,
            "division": division.model_dump(mode="json"),
            "problems": [_problem_payload(p, solve_statuses) for p in problems],
            "emergency_notice": contest_payload["emergency_notice"],
            "is_preview": bool(participant and participant.get("is_preview")),
        },
    )


@router.get("/contests/{contest_id}/problems")
@request_database
def problems(contest_id: str, request: Request):
    participant, _ = _allow_problem_view(request, contest_id)
    divisions = store.contest_divisions(contest_id)
    division_id = participant["division"].division_id if participant else (divisions[0].division_id if divisions else None)
    solve_statuses = _problem_solve_statuses(contest_id, participant)
    return page(
        request,
        [_problem_payload(p, solve_statuses) for p in _sort_problems(store.contest_problems(contest_id, division_id))],
    )


@router.get("/contests/{contest_id}/divisions/{division_id}/problems")
@request_database
def division_problems(contest_id: str, division_id: str, request: Request):
    participant, _ = _allow_problem_view(request, contest_id, division_id)
    if not store.get_division(contest_id, division_id):
        raise not_found()
    solve_statuses = _problem_solve_statuses(contest_id, participant)
    return page(
        request,
        [_problem_payload(p, solve_statuses) for p in _sort_problems(store.contest_problems(contest_id, division_id))],
    )


@router.get("/contests/{contest_id}/problems/{problem_id}")
@request_database
def problem_detail(contest_id: str, problem_id: str, request: Request):
    problem = store.get_problem(problem_id)
    if not problem or problem.contest_id != contest_id:
        raise not_found()
    participant, contest = _allow_problem_view(request, contest_id, problem.division_id)
    return ok(
        request,
        _problem_payload(
            problem,
            _problem_solve_statuses(contest_id, participant),
            include_editorial=_allow_editorial_view(contest, participant),
        ),
    )


@router.get("/contests/{contest_id}/problems/{problem_id}/assets")
@request_database
def problem_assets(contest_id: str, problem_id: str, request: Request):
    problem = store.get_problem(problem_id)
    if not problem or problem.contest_id != contest_id:
        raise not_found()
    participant, contest = _allow_problem_view(request, contest_id, problem.division_id)
    include_editorial_assets = _allow_editorial_view(contest, participant)
    try:
        assets = [
            asset
            for asset in store.problem_assets_for_problem(contest_id, problem_id)
            if "/package-files/" not in asset.storage_key
            and "/support/" not in asset.storage_key
            and "/verification-solutions/" not in asset.storage_key
            # Older Editor exports placed their private source snapshot in assets.
            and not is_editor_source_asset(asset)
            and (
                include_editorial_assets
                or "/editorial-assets/" not in asset.storage_key
            )
        ]
    except ValueError:
        raise not_found()
    return page(
        request,
        [{**asset.model_dump(mode="json"), "download_url": object_storage.presigned_get_url(asset.storage_key)} for asset in assets],
    )


@router.post("/contests/{contest_id}/problems/{problem_id}/submissions")
def create_submission(contest_id: str, problem_id: str, payload: SubmissionCreateRequest, request: Request):
    participant = require_participant(request, contest_id)
    contest = store.get_contest(contest_id)
    if not contest:
        raise not_found()
    if not participant.get("is_preview") and (contest.status != ContestStatus.RUNNING or now_utc() >= contest.end_at):
        raise invalid_state("Contest is not accepting submissions.")
    if payload.language not in set(LANGUAGES):
        raise AppError(422, "validation_error", "Unsupported language.", {"fields": [{"path": "body.language", "code": "invalid_enum"}]})
    problem = store.get_problem(problem_id)
    if not problem or problem.contest_id != contest_id:
        raise not_found()
    require_allowed(problem, payload.language)
    try:
        submission = (
            create_preview_submission(contest_id, problem_id, participant, payload.language, payload.source_code)
            if participant.get("is_preview") else
            store.create_submission(contest_id, problem_id, str(participant["member"].email), payload.language, payload.source_code)
        )
    except ValueError as error:
        if "division mismatch" in str(error):
            raise not_found("Problem is not available for this participant division.")
        raise not_found("Participant email or problem is not registered.")
    return ok(request, _participant_submission_payload(submission, include_source=False, show_progress=contest.participant_progress_visible))


@router.post("/contests/{contest_id}/problems/{problem_id}/mock-submissions")
def create_mock_submission(contest_id: str, problem_id: str, payload: SubmissionCreateRequest, request: Request):
    problem = store.get_problem(problem_id)
    if not problem or problem.contest_id != contest_id:
        raise not_found()
    participant, contest = _allow_problem_view(request, contest_id, problem.division_id)
    if participant and participant.get("is_preview"):
        raise permission_denied("미리보기에서는 참가자 제출 기능을 사용해 주세요.")
    if not _is_ended(contest) or not contest.mock_judging_enabled:
        raise not_found()
    if contest.problem_access_after_end == ContestResourceAccess.PRIVATE:
        raise not_found()
    if contest.problem_access_after_end == ContestResourceAccess.PARTICIPANTS and not participant:
        raise authentication_required("Participant access token is required.")
    if payload.language not in set(LANGUAGES):
        raise AppError(422, "validation_error", "Unsupported language.", {"fields": [{"path": "body.language", "code": "invalid_enum"}]})
    if not payload.source_code.strip():
        raise AppError(422, "validation_error", "Source code is required.")
    try:
        submission = store.create_mock_submission(contest_id, problem_id, payload.language, payload.source_code)
    except ValueError:
        raise not_found()
    show_progress = contest.participant_progress_visible or contest.mock_judging_progress_visible
    return ok(request, _mock_submission_payload(submission, show_progress=show_progress))


@router.get("/contests/{contest_id}/mock-submissions/{submission_id}/status:wait")
async def wait_mock_submission_status(
    contest_id: str,
    submission_id: str,
    request: Request,
    wait_seconds: float = 2.0,
    poll_interval_seconds: float = 0.25,
):
    @request_database(session_factory=status_session)
    def snapshot():
        submission = store.get_submission(submission_id, include_source=False)
        if not submission or submission.contest_id != contest_id or not _is_mock_submission(submission):
            raise not_found()
        problem = store.get_problem(submission.problem_id)
        if not problem:
            raise not_found()
        participant, contest = _allow_problem_view(request, contest_id, problem.division_id)
        if participant and participant.get("is_preview"):
            raise permission_denied("미리보기에서는 참가자 제출 기능을 사용해 주세요.")
        if not _is_ended(contest) or not contest.mock_judging_enabled:
            raise not_found()
        if contest.problem_access_after_end == ContestResourceAccess.PARTICIPANTS and not participant:
            raise authentication_required("Participant access token is required.")
        return submission, _mock_submission_payload(submission,
            show_progress=contest.participant_progress_visible or contest.mock_judging_progress_visible)

    submission, payload = await read_status_snapshot(snapshot)
    if submission.status in PENDING:
        await wait_for_submission(submission_id, min(max(wait_seconds, 0), 10))
        submission, payload = await read_status_snapshot(snapshot)
    return ok(request, payload)


@router.get("/contests/{contest_id}/notices")
@request_database
def contest_notices(contest_id: str, request: Request):
    participant = _optional_participant(request, contest_id)
    contest = store.get_contest(contest_id) if participant and participant.get("is_preview") else store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    ended = _is_ended(contest)
    if ended and not _allow_visible_resource(contest.notice_access_after_end, participant):
        raise not_found()
    include_participant_visible = (
        ended
        and contest.notice_access_after_end == ContestResourceAccess.PUBLIC
    )
    return page(
        request,
        [
            notice.model_dump(mode="json")
            for notice in store.contest_notices_for_view(
                contest_id,
                participant,
                include_participant_visible=include_participant_visible,
            )
        ],
    )


@router.get("/contests/{contest_id}/boards")
@request_database
def contest_board(contest_id: str, request: Request):
    participant = _optional_participant(request, contest_id)
    contest = store.get_contest(contest_id) if participant and participant.get("is_preview") else store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    if participant and participant.get("is_preview"):
        return page(request, [question.model_dump(mode="json") for question in preview_questions(participant)])
    if _is_ended(contest) and not _allow_visible_resource(contest.board_access_after_end, participant):
        raise not_found()
    return page(request, [question.model_dump(mode="json") for question in store.questions_for_view(contest_id, participant)])


@router.post("/contests/{contest_id}/boards")
def create_question(contest_id: str, payload: QuestionCreateRequest, request: Request):
    participant = require_participant(request, contest_id)
    if participant.get("is_preview"):
        if payload.visibility not in {"public", "private"}:
            raise AppError(422, "validation_error", "Unsupported question visibility.")
        return ok(request, create_preview_question(participant, payload.title, payload.body, payload.visibility).model_dump(mode="json"))
    contest = store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    if _is_ended(contest) and not _allow_board_write_after_end(contest, participant):
        raise not_found()
    if payload.visibility not in {"public", "private"}:
        raise AppError(422, "validation_error", "Unsupported question visibility.")
    question = store.create_question(contest_id, participant, payload.title, payload.body, payload.visibility)
    if contest:
        operator_accounts = [
            account
            for account in store.contest_operator_accounts(contest_id)
            if not account.is_service_master and has_contest_permission(account, contest_id, "contest.board.question.manage")
        ]
        subject = f"[ZOJ] 새 질문 · {contest.title}"
        question_url = absolute_url(f"/operator/contests/{contest_id}/board?questionId={question.contest_question_id}")
        visibility_label = "전체 공개" if question.visibility == "public" else "비공개"
        body_lines = [
            f"대회: {contest.title}",
            f"유형: {participant['division'].name}",
            f"팀: {participant['team'].team_name}",
            f"작성자: {participant['member'].name} <{participant['member'].email}>",
            f"공개 범위: {visibility_label}",
            f"제목: {question.title}",
            "",
            "질문 본문:",
            question.body,
            "",
            f"바로가기: {question_url}",
        ]
        notified = set()
        for account in operator_accounts:
            email = str(account.email).strip().lower()
            if not email or email in notified:
                continue
            notified.add(email)
            store.enqueue_mail(
                "contest_question_created",
                email,
                subject,
                "\n".join(body_lines),
                render_branded_email(
                    eyebrow="새 질문 알림",
                    title="새 질문이 등록되었습니다",
                    preheader=question.title,
                    body=[f"{contest.title}에 새 질문이 등록되었습니다."],
                    meta=[
                        ("대회", contest.title),
                        ("유형", participant["division"].name),
                        ("팀", participant["team"].team_name),
                        ("작성자", f"{participant['member'].name} <{participant['member'].email}>"),
                        ("공개 범위", visibility_label),
                        ("제목", question.title),
                    ],
                    sections=[("질문 본문", question.body)],
                    button_label="질문 확인하기",
                    button_url=question_url,
                ),
                contest_id=contest_id,
            )
    return ok(request, question.model_dump(mode="json"))


@router.post("/contests/{contest_id}/boards/{question_id}/answers")
def create_question_answer(
    contest_id: str,
    question_id: str,
    payload: QuestionAnswerCreateRequest,
    request: Request,
):
    participant = require_participant(request, contest_id)
    if participant.get("is_preview"):
        if not payload.body.strip():
            raise AppError(422, "validation_error", "Answer body is required.")
        answer = create_preview_answer(participant, question_id, payload.body.strip())
        if not answer:
            raise not_found()
        return ok(request, answer.model_dump(mode="json"))
    contest = store.get_public_contest(contest_id, allow_private=bool(participant))
    if not contest:
        raise not_found()
    if _is_ended(contest) and not _allow_board_write_after_end(contest, participant):
        raise not_found()

    question = store.get_contest_question(contest_id, question_id)
    if not question:
        raise not_found()
    if question.visibility == "private" and question.participant_team_id != participant["team"].participant_team_id:
        raise not_found()

    body = payload.body.strip()
    if not body:
        raise AppError(422, "validation_error", "Answer body is required.")

    answer = store.create_answer(
        contest_id,
        question_id,
        body,
        "public",
        str(participant["member"].email),
    )
    if not answer:
        raise not_found()
    return ok(request, answer.model_dump(mode="json"))


@router.get("/contests/{contest_id}/submissions")
@request_database
def submissions(
    contest_id: str,
    request: Request,
    limit: int = 100,
    cursor: str | None = None,
    include_source: bool = False,
    division_id: str | None = None,
    problem_id: str | None = None,
):
    participant, contest = _allow_submission_list_view(request, contest_id)
    show_progress = _submission_progress_visible(contest)
    if participant and participant.get("is_preview"):
        if division_id and division_id != participant["division"].division_id:
            raise not_found()
        items, next_cursor, total_count = list_preview_submissions(participant, problem_id=problem_id, limit=limit, cursor=cursor)
        return page(request, [_participant_submission_payload(item, include_source=include_source, show_progress=show_progress, team_name=participant["team"].team_name, member_name=participant["member"].name) for item in items], next_cursor=next_cursor, limit=max(1, min(limit, 300)), total_count=total_count, current_cursor=cursor)
    if not participant or _is_ended(contest):
        submissions, next_cursor, total_count = store.list_submissions(
            contest_id=contest_id,
            division_id=division_id,
            problem_id=problem_id,
            exclude_operator_tests=True,
            include_source=False,
            limit=limit,
            cursor=cursor,
        )
        source_lengths = store.submission_source_lengths([submission.submission_id for submission in submissions])
        items = _participant_submission_payloads(
            submissions,
            include_source=False,
            queue_ranks=store.pending_queue_ranks(contest_id, submission_ids=[s.submission_id for s in submissions if s.status == "waiting"]),
            source_lengths=source_lengths,
            show_progress=show_progress,
        )
        return page(
            request,
            items,
            next_cursor=next_cursor,
            limit=max(1, min(limit, 300)),
            total_count=total_count,
            current_cursor=cursor,
        )
    submissions, next_cursor, total_count = store.list_submissions(
        contest_id=contest_id,
        problem_id=problem_id,
        participant_team_id=participant["team"].participant_team_id,
        include_source=include_source,
        limit=limit,
        cursor=cursor,
    )
    source_lengths = ({s.submission_id: len(s.source_code.encode()) for s in submissions}
        if include_source else store.submission_source_lengths([s.submission_id for s in submissions]))
    items = _participant_submission_payloads(
        submissions,
        include_source=include_source,
        queue_ranks=store.pending_queue_ranks(contest_id, submission_ids=[s.submission_id for s in submissions if s.status == "waiting"]),
        source_lengths=source_lengths,
        show_progress=show_progress,
    )
    return page(
        request,
        items,
        next_cursor=next_cursor,
        limit=max(1, min(limit, 300)),
        total_count=total_count,
        current_cursor=cursor,
    )


@router.get("/contests/{contest_id}/submissions/{submission_id}")
@request_database
def submission_detail(contest_id: str, submission_id: str, request: Request):
    participant = _require_contest_participant(request, contest_id)
    contest = store.get_contest(contest_id)
    if not contest:
        raise not_found()
    submission = store.get_submission(submission_id)
    if not submission or submission.contest_id != contest_id or not owns_submission(participant, submission):
        raise not_found()
    return ok(request, _participant_submission_payload(submission, include_source=True, show_progress=_submission_progress_visible(contest)))


@router.get("/contests/{contest_id}/submissions/{submission_id}/status:wait")
async def wait_submission_status(
    contest_id: str,
    submission_id: str,
    request: Request,
    wait_seconds: float = 2.0,
    poll_interval_seconds: float = 0.25,
):
    # Authenticate/authorize each HTTP request in a worker thread. Waiting itself
    # shares indexed status batches, rather than one SELECT per tab per tick.
    @request_database(session_factory=status_session)
    def snapshot():
        participant = _require_contest_participant(request, contest_id)
        contest = store.get_contest(contest_id)
        submission = store.get_submission(submission_id)
        if not contest or not submission or submission.contest_id != contest_id or not owns_submission(participant, submission):
            raise not_found()
        return submission, _participant_submission_payload(submission, include_source=True,
            show_progress=_submission_progress_visible(contest))

    submission, payload = await read_status_snapshot(snapshot)
    if submission.status in PENDING:
        await wait_for_submission(submission_id, min(max(wait_seconds, 0), 10))
        submission, payload = await read_status_snapshot(snapshot)
    return ok(request, payload)


@router.get("/contests/{contest_id}/scoreboard")
@request_database
def scoreboard(contest_id: str, request: Request):
    participant, _ = _allow_scoreboard_view(request, contest_id)
    divisions = store.contest_divisions(contest_id)
    division = participant["division"] if participant else (divisions[0] if divisions else None)
    if not division:
        raise not_found("Contest division is not configured.")
    board = store.scoreboard_rows(contest_id, division.division_id, public_view=True, serialized=True, response_division=division)
    if not board or board == "null":
        raise not_found()
    return scoreboard_response(request, board)


@router.get("/contests/{contest_id}/scoreboard:wait")
async def wait_scoreboard(contest_id: str, request: Request, wait_seconds: float = 2.0):
    await asyncio.sleep(max(0, min(wait_seconds, 10.0)))
    return await run_in_threadpool(scoreboard, contest_id, request)


@router.get("/contests/{contest_id}/divisions/{division_id}/scoreboard")
@request_database
def division_scoreboard(contest_id: str, division_id: str, request: Request):
    _allow_scoreboard_view(request, contest_id, division_id)
    division = store.get_division(contest_id, division_id)
    if not division:
        raise not_found()
    board = store.scoreboard_rows(contest_id, division_id, public_view=True, serialized=True, response_division=division)
    if not board or board == "null":
        raise not_found()
    return scoreboard_response(request, board)


@router.get("/contests/{contest_id}/divisions/{division_id}/scoreboard:wait")
async def wait_division_scoreboard(contest_id: str, division_id: str, request: Request, wait_seconds: int = 5):
    await asyncio.sleep(max(0, min(wait_seconds, 10)))
    return await run_in_threadpool(division_scoreboard, contest_id, division_id, request)
