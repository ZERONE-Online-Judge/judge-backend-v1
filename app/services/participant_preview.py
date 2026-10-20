"""Participant-route previews without real teams, shared board posts or scoring."""
from datetime import timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    ContestQuestion, ContestQuestionAnswer, ParticipantTeam, SubmissionStatus,
    TeamMember, TeamMemberRole, now_utc,
)
from app.orm_models import (
    ContestRow, ContestDivisionRow, GeneralSessionRow, JudgeJobRow, ParticipantPreviewQuestionRow,
    ParticipantPreviewSessionRow, ProblemRow, StaffAccountRow, SubmissionRow,
)
from app.services.security import decode_session_token, token_hash

PREVIEW_SCOPE = "contest.participant.preview"
PREVIEW_KIND = "participant_preview"


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _account_session(db, contest_id: str, access_token: str):
    if not db.get(ContestRow, contest_id):
        return None
    if not decode_session_token(access_token, "general_access"):
        return None
    session = db.scalar(select(GeneralSessionRow).where(
        GeneralSessionRow.access_token_hash == token_hash(access_token),
        GeneralSessionRow.revoked_at.is_(None),
    ))
    if not session or _aware(session.access_expires_at) <= now_utc():
        return None
    account = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == session.email.lower()))
    if not account:
        return None
    from app.services.authz import has_contest_permission
    from app.services.store import _staff

    if not has_contest_permission(_staff(account), contest_id, PREVIEW_SCOPE):
        return None
    return account, session


def preview_account(contest_id: str, access_token: str):
    from app.services.store import _staff
    with SessionLocal() as db:
        pair = _account_session(db, contest_id, access_token)
        return _staff(pair[0]) if pair else None


def _identity(account, division):
    from app.services.store import _division
    member = TeamMember(
        team_member_id=f"preview-member:{account.staff_account_id}:{division.division_id}",
        name=account.display_name, email=account.email, role=TeamMemberRole.LEADER,
    )
    team = ParticipantTeam(
        participant_team_id=f"preview-team:{account.staff_account_id}:{division.division_id}",
        contest_id=division.contest_id, division_id=division.division_id,
        team_name=f"{account.display_name} · 참가자 미리보기", status="preview", members=[member],
    )
    return {"team": team, "member": member, "division": _division(division), "is_preview": True, "preview_staff_id": account.staff_account_id}


def select_preview_division(contest_id: str, division_id: str, access_token: str):
    with SessionLocal() as db:
        pair = _account_session(db, contest_id, access_token)
        if not pair:
            return None
        account, session = pair
        division = db.get(ContestDivisionRow, division_id)
        if not division or division.contest_id != contest_id:
            raise ValueError("division not found")
        selection = db.get(ParticipantPreviewSessionRow, (session.general_session_id, contest_id))
        if not selection:
            selection = ParticipantPreviewSessionRow(
                general_session_id=session.general_session_id, contest_id=contest_id,
                staff_account_id=account.staff_account_id, division_id=division_id,
            )
            db.add(selection)
        else:
            selection.division_id = division_id
        session.last_seen_at = now_utc()
        db.commit()
        return _identity(account, division)


def get_preview_participant(contest_id: str, access_token: str):
    with SessionLocal() as db:
        pair = _account_session(db, contest_id, access_token)
        if not pair:
            return None
        account, session = pair
        selection = db.get(ParticipantPreviewSessionRow, (session.general_session_id, contest_id))
        if not selection or selection.staff_account_id != account.staff_account_id:
            return None
        division = db.get(ContestDivisionRow, selection.division_id)
        if not division or division.contest_id != contest_id:
            return None
        return _identity(account, division)


def owns_submission(participant: dict, submission) -> bool:
    if not submission:
        return False
    if not participant.get("is_preview"):
        return submission.participant_team_id == participant["team"].participant_team_id
    return (
        submission.submission_kind == PREVIEW_KIND
        and submission.contest_id == participant["team"].contest_id
        and submission.division_id == participant["division"].division_id
        and str(submission.submitted_by_email or "").lower() == str(participant["member"].email).lower()
    )


def create_preview_submission(contest_id: str, problem_id: str, participant: dict, language: str, source_code: str):
    from app.services.store import _normalize_source_code, _submission
    with SessionLocal() as db:
        problem = db.get(ProblemRow, problem_id)
        if not problem or problem.contest_id != contest_id or problem.division_id != participant["division"].division_id:
            raise ValueError("division mismatch")
        submission = SubmissionRow(
            contest_id=contest_id, division_id=problem.division_id, problem_id=problem_id,
            participant_team_id=None, team_member_id=None, submission_kind=PREVIEW_KIND,
            submitted_by_name=participant["member"].name, submitted_by_email=str(participant["member"].email),
            language=language, source_code=_normalize_source_code(source_code), status=SubmissionStatus.WAITING.value,
        )
        db.add(submission)
        db.flush()
        next_position = (db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0) + 1
        db.add(JudgeJobRow(submission_id=submission.submission_id, contest_id=contest_id, division_id=problem.division_id, status="pending", queue_position=next_position))
        db.commit()
        db.refresh(submission)
        return _submission(submission)


def list_preview_submissions(participant: dict, *, problem_id: str | None = None, limit: int = 100, cursor: str | None = None):
    from app.services.store import _submission
    safe_limit = max(1, min(limit, 300))
    try:
        offset = max(0, int(cursor or "0"))
    except ValueError:
        offset = 0
    filters = [
        SubmissionRow.submission_kind == PREVIEW_KIND,
        SubmissionRow.contest_id == participant["team"].contest_id,
        SubmissionRow.division_id == participant["division"].division_id,
        func.lower(SubmissionRow.submitted_by_email) == str(participant["member"].email).lower(),
    ]
    if problem_id:
        filters.append(SubmissionRow.problem_id == problem_id)
    with SessionLocal() as db:
        total = int(db.scalar(select(func.count()).select_from(SubmissionRow).where(*filters)) or 0)
        rows = db.scalars(select(SubmissionRow).where(*filters).order_by(SubmissionRow.submitted_at.desc(), SubmissionRow.submission_id.desc()).offset(offset).limit(safe_limit)).all()
        next_cursor = str(offset + safe_limit) if offset + safe_limit < total else None
        return [_submission(row) for row in rows], next_cursor, total


def preview_solve_statuses(participant: dict) -> dict[str, str]:
    with SessionLocal() as db:
        rows = db.execute(select(SubmissionRow.problem_id, SubmissionRow.status).where(
            SubmissionRow.submission_kind == PREVIEW_KIND,
            SubmissionRow.contest_id == participant["team"].contest_id,
            SubmissionRow.division_id == participant["division"].division_id,
            func.lower(SubmissionRow.submitted_by_email) == str(participant["member"].email).lower(),
        )).all()
    statuses = {}
    for problem_id, status in rows:
        if statuses.get(problem_id) == "accepted":
            continue
        if status == "accepted":
            statuses[problem_id] = "accepted"
        elif status not in {"waiting", "preparing", "judging"}:
            statuses[problem_id] = "wrong"
    return statuses


def preview_questions(participant: dict) -> list[ContestQuestion]:
    from app.services.store import store
    contest_id = participant["team"].contest_id
    # Public existing posts appear exactly as they do to this participant, while
    # any preview replies remain in a private snapshot for this preview account.
    questions = {question.contest_question_id: question for question in store.questions_for_view(contest_id, participant)}
    with SessionLocal() as db:
        rows = db.scalars(select(ParticipantPreviewQuestionRow).where(
            ParticipantPreviewQuestionRow.contest_id == contest_id,
            ParticipantPreviewQuestionRow.division_id == participant["division"].division_id,
            ParticipantPreviewQuestionRow.staff_account_id == participant["preview_staff_id"],
        )).all()
        for row in rows:
            if row.source_question_id and row.source_question_id not in questions:
                continue
            question = ContestQuestion.model_validate_json(row.payload)
            questions[question.contest_question_id] = question
    return sorted(questions.values(), key=lambda question: question.created_at, reverse=True)


def create_preview_question(participant: dict, title: str, body: str, visibility: str):
    question = ContestQuestion(
        contest_id=participant["team"].contest_id,
        participant_team_id=participant["team"].participant_team_id,
        team_member_id=participant["member"].team_member_id, title=title, body=body,
        visibility=visibility, team_name=participant["team"].team_name,
        division_name=participant["division"].name, author_name=participant["member"].name,
        author_email=participant["member"].email,
    )
    with SessionLocal() as db:
        db.add(ParticipantPreviewQuestionRow(
            question_id=question.contest_question_id, contest_id=question.contest_id,
            staff_account_id=participant["preview_staff_id"], division_id=participant["division"].division_id,
            payload=question.model_dump_json(),
        ))
        db.commit()
    return question


def create_preview_answer(participant: dict, question_id: str, body: str):
    question = next((item for item in preview_questions(participant) if item.contest_question_id == question_id), None)
    if not question:
        return None
    answer = ContestQuestionAnswer(
        contest_question_id=question_id, contest_id=question.contest_id, body=body,
        created_by_email=participant["member"].email, created_by_name=participant["member"].name,
        created_by_role="participant", created_by_team_name=participant["team"].team_name,
        created_by_division_name=participant["division"].name,
    )
    with SessionLocal() as db:
        key = (participant["preview_staff_id"], participant["division"].division_id, question_id)
        row = db.get(ParticipantPreviewQuestionRow, key)
        if row:
            question = ContestQuestion.model_validate_json(row.payload)
        else:
            row = ParticipantPreviewQuestionRow(
                staff_account_id=key[0], division_id=key[1], question_id=question_id,
                contest_id=question.contest_id, source_question_id=question_id,
            )
            db.add(row)
        question.answers.append(answer)
        question.updated_at = now_utc()
        row.payload = question.model_dump_json()
        db.commit()
    return answer
