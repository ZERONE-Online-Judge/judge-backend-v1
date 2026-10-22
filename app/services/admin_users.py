"""Read-only service-master directory across login and contest identities."""
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, union

from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import (
    AccountPasswordRow, ContestDivisionRow, ContestRow, IdentityAccountRow,
    ParticipantTeamRow, StaffAccountRow, SubmissionRow, TeamMemberRow,
)
from app.services.contest_roles import title_for_roles, title_for_scopes
from app.services.errors import not_found
from app.services.store import OPERATOR_TEST_TEAM_PREFIX, _aware, _schedule_status


def _members():
    return (
        select(TeamMemberRow, ParticipantTeamRow, ContestRow, ContestDivisionRow)
        .join(ParticipantTeamRow, TeamMemberRow.participant_team_id == ParticipantTeamRow.participant_team_id)
        .join(ContestRow, TeamMemberRow.contest_id == ContestRow.contest_id)
        .join(ContestDivisionRow, ParticipantTeamRow.division_id == ContestDivisionRow.division_id)
        .where(ContestRow.deleted_at.is_(None),
               ~ParticipantTeamRow.team_name.startswith(OPERATOR_TEST_TEAM_PREFIX, autoescape=True))
    )


def _directory():
    # Select safe columns only. Never load password hashes or sessions.
    members = _members().with_only_columns(
        func.lower(TeamMemberRow.email).label("email"), TeamMemberRow.name.label("name"),
        maintain_column_froms=True)
    sources = union(
        select(func.lower(IdentityAccountRow.email).label("email"), IdentityAccountRow.display_name.label("name")),
        select(func.lower(StaffAccountRow.email).label("email"), StaffAccountRow.display_name.label("name")),
        members).subquery()
    return select(sources.c.email).where(
        sources.c.email != "", ~sources.c.email.endswith("@local.zoj", autoescape=True),
        ~sources.c.email.startswith("operator-test+", autoescape=True)), sources


def _summaries(db, emails):
    users = {email: dict(email=email, display_name=email, names=[], has_password=False,
                        password_updated_at=None, account_created_at=None,
                        roles=[], participant_contest_count=0) for email in emails}
    if not users:
        return users
    for row in db.scalars(select(IdentityAccountRow).where(func.lower(IdentityAccountRow.email).in_(emails))):
        user = users[row.email.lower()]
        user.update(display_name=row.display_name, account_created_at=_aware(row.created_at))
        user["names"].append(row.display_name)
    for row in db.scalars(select(StaffAccountRow).where(func.lower(StaffAccountRow.email).in_(emails))):
        user = users[row.email.lower()]
        if user["display_name"] == user["email"]:
            user["display_name"] = row.display_name
        user["names"].append(row.display_name)
        user["roles"].append("service_master" if row.is_service_master else "operator")
    contests = {email: set() for email in emails}
    for member, team, contest, division in db.execute(_members().where(func.lower(TeamMemberRow.email).in_(emails)).order_by(ContestRow.start_at.desc(), ContestRow.contest_id)):
        user = users[member.email.lower()]
        if user["display_name"] == user["email"]:
            user["display_name"] = member.name
        user["names"].append(member.name)
        contests[member.email.lower()].add(contest.contest_id)
    for email, ids in contests.items():
        if ids:
            users[email]["roles"].append("participant")
        users[email]["participant_contest_count"] = len(ids)
    for email, updated_at in db.execute(select(AccountPasswordRow.email, AccountPasswordRow.updated_at).where(func.lower(AccountPasswordRow.email).in_(emails))):
        users[email.lower()].update(has_password=True, password_updated_at=datetime.fromtimestamp(updated_at, timezone.utc))
    for user in users.values():
        user["names"] = sorted(set(name for name in user["names"] if name))
        if not user["roles"]:
            user["roles"] = ["general"]
    return users


def _ordered_users(query, sort: str, order: str):
    matches = query.distinct().subquery()
    ordered = select(matches.c.email)
    value = matches.c.email
    if sort == "name":
        identities = select(
            func.lower(IdentityAccountRow.email).label("email"),
            func.min(IdentityAccountRow.display_name).label("name"),
        ).group_by(func.lower(IdentityAccountRow.email)).subquery()
        staff = select(
            func.lower(StaffAccountRow.email).label("email"),
            func.min(StaffAccountRow.display_name).label("name"),
        ).group_by(func.lower(StaffAccountRow.email)).subquery()
        members = _members().with_only_columns(
            func.lower(TeamMemberRow.email).label("email"), TeamMemberRow.name,
            func.row_number().over(
                partition_by=func.lower(TeamMemberRow.email),
                order_by=(ContestRow.start_at.desc(), ContestRow.contest_id),
            ).label("position"),
            maintain_column_froms=True,
        ).where(TeamMemberRow.name != func.lower(TeamMemberRow.email)).subquery()
        ordered = (
            ordered.outerjoin(identities, identities.c.email == matches.c.email)
            .outerjoin(staff, staff.c.email == matches.c.email)
            .outerjoin(members, (members.c.email == matches.c.email) & (members.c.position == 1))
        )
        # Match the display-name precedence used in the returned summaries.
        value = func.lower(func.coalesce(
            func.nullif(identities.c.name, matches.c.email),
            func.nullif(staff.c.name, matches.c.email), members.c.name, matches.c.email,
        ))
    elif sort == "contest_count":
        counts = _members().with_only_columns(
            func.lower(TeamMemberRow.email).label("email"),
            func.count(func.distinct(ContestRow.contest_id)).label("count"),
            maintain_column_froms=True,
        ).group_by(func.lower(TeamMemberRow.email)).subquery()
        ordered = ordered.outerjoin(counts, counts.c.email == matches.c.email)
        value = func.coalesce(counts.c.count, 0)
    elif sort == "created_at":
        identities = select(
            func.lower(IdentityAccountRow.email).label("email"),
            func.min(IdentityAccountRow.created_at).label("created_at"),
        ).group_by(func.lower(IdentityAccountRow.email)).subquery()
        ordered = ordered.outerjoin(identities, identities.c.email == matches.c.email)
        value = identities.c.created_at
    elif sort == "password_updated_at":
        passwords = select(
            func.lower(AccountPasswordRow.email).label("email"),
            func.max(AccountPasswordRow.updated_at).label("updated_at"),
        ).group_by(func.lower(AccountPasswordRow.email)).subquery()
        ordered = ordered.outerjoin(passwords, passwords.c.email == matches.c.email)
        value = passwords.c.updated_at
    # Missing dates stay last in either direction; email breaks every tie.
    direction = value.desc() if order == "desc" else value.asc()
    return ordered.order_by(direction.nulls_last(), matches.c.email.asc())


def list_users(q: str, password: str, limit: int, offset: int, sort: str = "email", order: str = "asc"):
    with SessionLocal() as db:
        query, sources = _directory()
        if q.strip():
            term = q.strip().lower()
            query = query.where(sources.c.email.contains(term, autoescape=True) | func.lower(sources.c.name).contains(term, autoescape=True))
        password_exists = select(AccountPasswordRow.email).where(func.lower(AccountPasswordRow.email) == sources.c.email).exists()
        if password != "all":
            query = query.where(password_exists if password == "set" else ~password_exists)
        query = query.distinct()
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        emails = list(db.scalars(_ordered_users(query, sort, order).limit(limit).offset(offset)))
        summaries = _summaries(db, emails)
        return [summaries[email] for email in emails], total


def user_detail(email: str):
    email = email.strip().lower()
    with SessionLocal() as db:
        query, sources = _directory()
        if db.scalar(query.where(sources.c.email == email).limit(1)) is None:
            raise not_found()
        user = _summaries(db, [email])[email]
        memberships = list(db.execute(_members().where(func.lower(TeamMemberRow.email) == email).order_by(ContestRow.start_at.desc(), ContestRow.contest_id)))
        member_ids = [member.team_member_id for member, *_ in memberships]
        counts = {}
        if member_ids:
            counts = {mid: (count, accepted) for mid, count, accepted in db.execute(
                select(SubmissionRow.team_member_id, func.count(), func.count().filter(SubmissionRow.status == "accepted"))
                .where(SubmissionRow.team_member_id.in_(member_ids), SubmissionRow.submission_kind == "participant")
                .group_by(SubmissionRow.team_member_id))}
        user["participations"] = [dict(
            contest_id=contest.contest_id, contest_title=contest.title,
            organization_name=contest.organization_name,
            contest_status=_schedule_status(contest.status, _aware(contest.start_at), _aware(contest.end_at), now_utc()),
            start_at=_aware(contest.start_at), end_at=_aware(contest.end_at),
            division_name=division.name, team_name=team.team_name,
            team_status=team.status, registered_name=member.name,
            member_role=member.role, registered_at=_aware(team.created_at),
            last_login_at=_aware(member.last_login_at),
            submission_count=counts.get(member.team_member_id, (0, 0))[0],
            accepted_submission_count=counts.get(member.team_member_id, (0, 0))[1],
        ) for member, team, contest, division in memberships]
        user["operator_contests"] = []
        staff = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == email))
        if staff:
            scopes, roles = json.loads(staff.contest_scopes or "{}"), json.loads(staff.contest_roles or "{}")
            # Service-wide master access does not imply contest participation.
            ids = [cid for cid, permissions in scopes.items() if permissions]
            for contest in db.scalars(select(ContestRow).where(ContestRow.contest_id.in_(ids)).order_by(ContestRow.start_at.desc(), ContestRow.contest_id)):
                user["operator_contests"].append(dict(contest_id=contest.contest_id,
                    contest_title=contest.title, display_name=staff.display_name,
                    role_title=title_for_roles(roles.get(contest.contest_id, [])) or title_for_scopes(scopes[contest.contest_id]) or "운영진"))
        return user
