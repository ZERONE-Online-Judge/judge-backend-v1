"""Transactional participant inbox, independent of active browser sessions."""
from datetime import timedelta
from urllib.parse import quote

from fastapi.encoders import jsonable_encoder
from sqlalchemy import case, delete, func, or_, select, text, update

from app.models import new_id, now_utc
from app.orm_models import (NotificationRow as Notice, ContestRow, ParticipantTeamRow,
    TeamMemberRow as Member, ProblemRow)
from app.services.errors import not_found
from app.settings import settings

LABELS = {'accepted': '맞았습니다', 'wrong_answer': '틀렸습니다', 'compile_error': '컴파일 에러',
    'runtime_error': '런타임 에러', 'time_limit_exceeded': '시간 초과',
    'memory_limit_exceeded': '메모리 초과', 'output_limit_exceeded': '출력 초과', 'system_error': '시스템 에러'}


def _expired_contests(now):
    # Draft/TBD dates are placeholders. Rescheduling automatically changes the
    # lifetime of existing notifications because expiry uses the current end_at.
    return select(ContestRow.contest_id).where(
        ContestRow.status.not_in(['draft', 'schedule_tbd']),
        ContestRow.end_at <= now - timedelta(hours=settings.notification_grace_hours),
    )


def _alive(now):
    return Notice.contest_id.not_in(_expired_contests(now))


def _contest_members(contest_id, team_id=None):
    query = select(Member).join(ParticipantTeamRow,
        Member.participant_team_id == ParticipantTeamRow.participant_team_id).where(
            Member.contest_id == contest_id, ~ParticipantTeamRow.team_name.startswith('__operator_test__'))
    return query.where(Member.participant_team_id == team_id) if team_id else query


def _emit(db, members, event_key, kind, contest_id, title, body, href, **details):
    # One bulk insert for all recipients; duplicate deliveries cannot recreate hidden/read events.
    contest = db.get(ContestRow, contest_id)
    from app.services.store import _aware
    current = now_utc()
    if not contest or (contest.status not in {'draft', 'schedule_tbd'} and
        _aware(contest.end_at) + timedelta(hours=settings.notification_grace_hours) <= current):
        return
    now = details.pop('created_at', None) or current
    rows = [dict(notification_id=new_id(), team_member_id=member.team_member_id,
        recipient_email=member.email.strip().lower(), event_key=event_key, notification_type=kind,
        contest_id=contest_id, contest_title=contest.title if contest else '대회',
        title=title, body=body, href=href, created_at=now, **details) for member in db.scalars(members)]
    if not rows:
        return
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(Notice).on_conflict_do_nothing(
        index_elements=['event_key', 'team_member_id']), rows)


def create_notice(db, notice):
    db.flush()
    _emit(db, _contest_members(notice.contest_id), f'notice:{notice.contest_notice_id}', 'notice',
        notice.contest_id, '새 대회 공지가 올라왔습니다', notice.title,
        f'/contests/{quote(notice.contest_id, safe="")}/board?noticeId={quote(notice.contest_notice_id, safe="")}',
        created_at=notice.published_at)


def create_answer(db, answer, question):
    db.flush()
    _emit(db, _contest_members(answer.contest_id, question.participant_team_id),
        f'answer:{answer.contest_answer_id}', 'answer', answer.contest_id,
        '질문에 답변이 등록됐습니다', question.title,
        f'/contests/{quote(answer.contest_id, safe="")}/board?questionId={quote(question.contest_question_id, safe="")}',
        created_at=answer.created_at)


def _authors(submission):
    if not submission.participant_team_id:
        return select(Member).where(False)
    query = _contest_members(submission.contest_id, submission.participant_team_id)
    if submission.team_member_id:
        return query.where(Member.team_member_id == submission.team_member_id)
    if submission.submitted_by_email:
        return query.where(func.lower(Member.email) == submission.submitted_by_email.lower())
    return query  # Legacy shared-team submissions have no individual author.


def _submission_href(submission):
    return f'/contests/{quote(submission.contest_id, safe="")}/problems/{quote(submission.problem_id, safe="")}?submissionId={quote(submission.submission_id, safe="")}'


def create_result(db, submission, job):
    if submission.submission_kind != 'participant':
        return
    problem = db.get(ProblemRow, submission.problem_id)
    code = problem.problem_code if problem else '문제'
    _emit(db, _authors(submission), f'submission:{job.judge_job_id}', 'submission', submission.contest_id,
        f'{code}에 대한 제출 결과가 나왔습니다',
        (f'{submission.score}/100점 · {LABELS.get(submission.status, submission.status)}'
            if submission.scoring_mode == 'ioi' and submission.score is not None else LABELS.get(submission.status, submission.status)),
        _submission_href(submission), submission_id=submission.submission_id, problem_id=submission.problem_id,
        problem_code=code, new_status=submission.status, created_at=submission.status_updated_at)


def create_rejudging(db, batch, item, submission, context):
    if submission.submission_kind != 'participant' or all(
        item.original_result.get(key) == getattr(submission, key) for key in ('status', 'score', 'subtask_results')):
        return
    code = context['problem']['problem_code']
    old, new = item.original_result['status'], submission.status
    title = f'{code} 판정이 변경됐습니다'
    body = f'{LABELS.get(old, old)} → {LABELS.get(new, new)}'
    if submission.scoring_mode == 'ioi':
        title = f'{code} 채점 결과가 변경됐습니다'
        body = f'{item.original_result.get("score", 0)}/100점 → {submission.score}/100점 · 서브태스크 결과를 확인하세요.'
    _emit(db, _authors(submission), f'rejudging:{item.rejudging_item_id}', 'rejudging', batch.contest_id,
        title, body,
        _submission_href(submission), rejudging_item_id=item.rejudging_item_id,
        submission_id=submission.submission_id, problem_id=batch.problem_id, problem_code=code,
        old_status=old, new_status=new, reason=batch.reason, created_at=submission.status_updated_at)


def _scope(db, token, contest_id=None):
    from app.services.rejudging_notifications import _owned_member_ids
    owned = _owned_member_ids(db, token, contest_id)
    # Snapshot the recipient identity so assigning a member to a different email
    # cannot transfer the previous account's private notification history.
    identity = select(func.lower(Member.email)).where(Member.team_member_id == Notice.team_member_id).scalar_subquery()
    return (Notice.team_member_id.in_(owned), or_(Notice.recipient_email.is_(None), Notice.recipient_email == identity))


def _fields(row):
    title, body, href = row.title, row.body, row.href
    if row.notification_type == 'rejudging' and not title:
        title = f'{row.problem_code} 판정이 변경됐습니다'
        body = f'{LABELS.get(row.old_status, row.old_status)} → {LABELS.get(row.new_status, row.new_status)}'
        href = _submission_href(row)
    data = {key: getattr(row, key) for key in ('notification_id', 'notification_type', 'contest_id',
        'contest_title', 'created_at', 'read_at', 'dismissed_at', 'reason', 'problem_id', 'problem_code',
        'submission_id', 'old_status', 'new_status')}
    return jsonable_encoder({**data, 'title': title, 'body': body, 'href': href})


def list_notifications(store, token, contest_id=None, offset=0, limit=50, kind=None):
    offset, limit = max(0, offset), max(1, min(limit, 100))
    with store._session() as db:
        filters = [*_scope(db, token, contest_id), Notice.deleted_at.is_(None), _alive(now_utc())]
        if kind:
            filters.append(Notice.notification_type == kind)
        unread, total = db.execute(select(func.count(case((Notice.read_at.is_(None), 1))), func.count())
            .select_from(Notice).where(*filters)).one()
        rows = list(db.scalars(select(Notice).where(*filters).order_by(
            Notice.created_at.desc(), Notice.notification_id.desc()).offset(offset).limit(limit + 1)))
        return {'items': [_fields(row) for row in rows[:limit]], 'unread_count': unread, 'total_count': total,
            'next_offset': offset + limit if len(rows) > limit else None}


def update_notification(store, token, notification_id, action, contest_id=None):
    with store._session() as db:
        filters = _scope(db, token, contest_id)
        visible = [] if action == "delete" else [Notice.deleted_at.is_(None)]
        visible.append(_alive(now_utc()))
        row = db.scalar(select(Notice).where(Notice.notification_id == notification_id,
            *filters, *visible).with_for_update())
        if row is None:
            raise not_found()
        now = now_utc()
        if action == 'unread':
            values = {'read_at': None}  # Do not reshow a previously dismissed toast.
        elif action in ('read', 'dismiss', 'delete'):
            column = {'read': 'read_at', 'dismiss': 'dismissed_at', 'delete': 'deleted_at'}[action]
            values = {column: func.coalesce(getattr(Notice, column), now)}
            if action == 'dismiss':
                values['read_at'] = func.coalesce(Notice.read_at, now)
        else:
            raise ValueError('Unknown notification action')
        db.execute(update(Notice).where(Notice.notification_id == notification_id).values(**values))
        db.commit()
        return {'notification_id': notification_id}


def read_all(store, token, contest_id=None):
    with store._session() as db:
        now = now_utc()
        result = db.execute(update(Notice).where(*_scope(db, token, contest_id),
            Notice.deleted_at.is_(None), Notice.read_at.is_(None), Notice.created_at <= now, _alive(now)).values(read_at=now))
        db.commit()
        return {'updated_count': result.rowcount}


def cleanup_expired_notifications(store):
    """Permanently remove one bounded batch, including read and hidden entries."""
    now = now_utc()
    cutoff = now - timedelta(hours=settings.notification_grace_hours)
    limit = max(1, min(settings.notification_cleanup_batch_size, 5000))
    with store._session() as db:
        if db.bind.dialect.name == 'postgresql':
            db.execute(text("SET LOCAL statement_timeout = '3s'"))
            db.execute(text("SET LOCAL lock_timeout = '500ms'"))
        # Lock both notifications and their contests during deletion. A concurrent
        # schedule extension cannot change an expiry halfway through this batch.
        ids = list(db.scalars(select(Notice.notification_id).join(ContestRow,
            Notice.contest_id == ContestRow.contest_id).where(
                ContestRow.status.not_in(['draft', 'schedule_tbd']), ContestRow.end_at <= cutoff,
            ).order_by(Notice.contest_id, Notice.notification_id).limit(limit)
            .with_for_update(skip_locked=True)))
        count = 0
        if ids:
            count = db.execute(delete(Notice).where(
                Notice.notification_id.in_(ids), Notice.contest_id.in_(_expired_contests(now)),
            )).rowcount
        db.commit()
        return count
