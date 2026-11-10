"""Shared, row-level participant import validation and read-only previews."""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import logging
from typing import Any

from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.orm_models import ContestDivisionRow, ParticipantTeamRow, TeamMemberRow


logger = logging.getLogger(__name__)


def name_without_controls(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Names must not contain control characters")
    return value


class ImportMember(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr

    _name_without_controls = field_validator("name")(name_without_controls)

    @field_validator("email", mode="before")
    @classmethod
    def plain_address(cls, value):
        if isinstance(value, str) and any(char in value for char in "<>"):
            raise ValueError("Enter an email address without a display name")
        return value


class ImportTeam(BaseModel):
    team_name: str = Field(min_length=1, max_length=255)
    division_id: str = Field(min_length=1, max_length=36)
    leader: ImportMember
    members: list[ImportMember]

    _name_without_controls = field_validator("team_name")(name_without_controls)


def issue(code, field, message, **details):
    return {"code": code, "field": field, "message": message, **details}


class ParticipantCreationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        first = issues[0]
        if first["code"] == "duplicate_email":
            message = "participant email already registered: " + first.get("email", "")
        elif first["code"] == "staff_email_conflict":
            message = "participant email cannot be operator/staff account: " + first.get("email", "")
        elif first["code"] == "invalid_division":
            message = "Contest division is not configured."
        else:
            message = first["message"]
        super().__init__(message)


@dataclass
class ImportState:
    divisions: set[str]
    team_names: set[str]
    emails: set[str]
    staff_emails: set[str]


def load_state(db, store, contest_id, emails):
    return ImportState(
        set(db.scalars(select(ContestDivisionRow.division_id).where(ContestDivisionRow.contest_id == contest_id))),
        set(db.scalars(select(ParticipantTeamRow.team_name).where(ParticipantTeamRow.contest_id == contest_id))),
        {value.strip().lower() for value in db.scalars(select(TeamMemberRow.email).where(TeamMemberRow.contest_id == contest_id))},
        set(store._contest_staff_email_conflicts(db, contest_id, emails)),
    )


def email_fields(leader_email, members):
    return [("leader.email", leader_email.strip().lower()), *[
        (f"members.{index}.email", email.strip().lower()) for index, (_, email) in enumerate(members)]]


def creation_issues(state, division_id, team_name, fields):
    errors = []
    if division_id not in state.divisions:
        errors.append(issue("invalid_division", "division_id", "이 대회에 등록된 참가 유형을 선택하세요."))
    if team_name in state.team_names:
        errors.append(issue("duplicate_team_name", "team_name", "이미 등록되었거나 앞선 행에서 사용한 팀명입니다."))
    counts = Counter(email for _, email in fields)
    for field, email in fields:
        if counts[email] > 1 or email in state.emails:
            errors.append(issue("duplicate_email", field, "이미 등록되었거나 다른 참가자와 중복된 이메일입니다.", email=email))
        if email in state.staff_emails:
            errors.append(issue("staff_email_conflict", field, "이 대회 운영자 또는 서비스 관리자 이메일은 참가자로 등록할 수 없습니다.", email=email))
    return errors


def normalize_row(raw: Any, row: int):
    cleaned = deepcopy(raw)
    corrections = []
    def clean(target, key, field, email=False):
        if not isinstance(target, dict) or not isinstance(target.get(key), str):
            return
        before = target[key]
        after = before.strip().lstrip("\ufeff").strip()
        if email:
            after = after.lower()
        target[key] = after
        if before != after:
            corrections.append({"field": field, "before": before, "after": after,
                "message": "앞뒤 공백과 이메일 표기를 정리했습니다." if email else "앞뒤 공백을 정리했습니다."})
    if isinstance(cleaned, dict):
        clean(cleaned, "team_name", "team_name")
        clean(cleaned, "division_id", "division_id")
        clean(cleaned.get("leader"), "name", "leader.name")
        clean(cleaned.get("leader"), "email", "leader.email", True)
        if isinstance(cleaned.get("members"), list):
            for index, member in enumerate(cleaned["members"]):
                clean(member, "name", f"members.{index}.name")
                clean(member, "email", f"members.{index}.email", True)
    errors, normalized = [], None
    try:
        normalized = ImportTeam.model_validate(cleaned).model_dump(mode="json")
        # EmailStr can additionally canonicalize an internationalized domain.
        # Every accepted change must remain visible before confirmation.
        for field, before, after in [("leader.email", cleaned["leader"]["email"], normalized["leader"]["email"]), *[
            (f"members.{index}.email", cleaned["members"][index]["email"], member["email"])
            for index, member in enumerate(normalized["members"])]]:
            if before != after:
                existing = next((item for item in corrections if item["field"] == field), None)
                if existing:
                    existing["after"] = after
                else:
                    corrections.append({"field": field, "before": before, "after": after,
                        "message": "이메일 표준 표기를 정리했습니다."})
    except ValidationError as error:
        for detail in error.errors(include_url=False, include_context=False, include_input=False):
            field = ".".join(str(part) for part in detail["loc"])
            if field.endswith(".email"):
                code, message = "invalid_email", "올바른 이메일 주소만 입력하세요. 주소나 도메인을 임의로 추측하여 수정하지 않습니다."
            elif detail["type"] in {"missing", "string_too_short"}:
                code, message = "required", "필수 항목을 입력하세요."
            elif detail["type"] == "string_too_long":
                code, message = "too_long", "입력 가능한 글자 수를 초과했습니다."
            elif detail["type"] == "value_error" and (field == "team_name" or field.endswith(".name")):
                code, message = "invalid_name", "팀명과 이름에는 줄바꿈이나 제어 문자를 입력할 수 없습니다."
            else:
                code, message = "invalid_value", "입력 형식을 확인하세요."
            errors.append(issue(code, field or "row", message))
    return {"row": row, "team_name": normalized["team_name"] if normalized else str(raw.get("team_name", "")) if isinstance(raw, dict) else "",
        "input": raw, "normalized": normalized, "valid": not errors, "errors": errors,
        "warnings": [], "corrections": corrections}


def summary(rows):
    ready = sum(row["valid"] for row in rows)
    return {"total": len(rows), "ready": ready, "invalid": len(rows) - ready,
        "corrected": sum(bool(row["corrections"]) for row in rows),
        "warnings": sum(len(row["warnings"]) for row in rows)}


def preview_participants(store, contest_id, teams):
    rows = [normalize_row(raw, index) for index, raw in enumerate(teams, start=1)]
    emails = [email for row in rows if row["normalized"] for _, email in email_fields(
        row["normalized"]["leader"]["email"], [(member["name"], member["email"]) for member in row["normalized"]["members"]])]
    with store._session() as db:
        state = load_state(db, store, contest_id, emails)
    for row in rows:
        team = row["normalized"]
        if team is None:
            continue
        fields = email_fields(team["leader"]["email"], [(member["name"], member["email"]) for member in team["members"]])
        row["errors"].extend(creation_issues(state, team["division_id"], team["team_name"], fields))
        row["valid"] = not row["errors"]
        if row["valid"]:
            state.team_names.add(team["team_name"])
            state.emails.update(email for _, email in fields)
    return {"rows": rows, "summary": summary(rows)}


def create_participants(store, contest_id, teams):
    created, errors, results = [], [], []
    for index, raw in enumerate(teams, start=1):
        row = normalize_row(raw, index)
        team = row["normalized"]
        if team is not None:
            try:
                division = store.get_division(contest_id, team["division_id"])
                saved = store.create_participant_team(contest_id=contest_id, division_id=team["division_id"],
                    team_name=team["team_name"], leader_name=team["leader"]["name"], leader_email=team["leader"]["email"],
                    members=[(member["name"], member["email"]) for member in team["members"]])
            except ParticipantCreationError as error:
                row["errors"].extend(error.issues)
            except IntegrityError:
                # The store's per-row session has rolled back before this
                # catch. Re-read to explain a concurrent uniqueness change.
                fields = email_fields(team["leader"]["email"], [(member["name"], member["email"]) for member in team["members"]])
                with store._session() as db:
                    state = load_state(db, store, contest_id, [email for _, email in fields])
                row["errors"].extend(creation_issues(state, team["division_id"], team["team_name"], fields)
                    or [issue("registration_conflict", "row", "등록 중 정보가 변경되었습니다. 내용을 확인한 뒤 실패한 행을 다시 등록하세요.")])
            except ValueError as error:
                row["errors"].append(issue("registration_error", "row", str(error)))
            else:
                # Team creation commits independently. A later invitation
                # failure must never ask the user to register that team again.
                created.append({**saved.model_dump(mode="json"), "division": division.model_dump(mode="json") if division else None})
                try:
                    store.enqueue_participant_invite_for_team(contest_id, saved.participant_team_id)
                except Exception:
                    logger.exception("Participant invitation enqueue failed for team %s", saved.participant_team_id)
                    row["warnings"].append(issue("invitation_failed", "row",
                        "팀 등록은 완료했지만 초대 메일을 예약하지 못했습니다. 참가자 목록에서 초대를 다시 보내세요."))
                results.append({**row, "status": "created", "participant_team_id": saved.participant_team_id})
                continue
        row["valid"] = False
        first = row["errors"][0]
        message = "participant email already registered" if first["code"] == "duplicate_email" else first["message"]
        errors.append({**row, "code": first["code"], "field": first["field"], "message": message})
        results.append({**row, "status": "failed"})
    return {"created": created, "errors": errors, "results": results,
        "summary": {"total": len(teams), "created": len(created), "failed": len(errors),
            "warnings": sum(len(row["warnings"]) for row in results)}}
