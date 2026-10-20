"""The single server-side contract for contest roles and their permissions."""
from pydantic import BaseModel, EmailStr, Field, field_validator

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"contest.*", "contest.owner"},
    "master": {"contest.*"},
    "settings_manager": {"contest.settings.manage", "contest.update_organization", "contest.update_overview", "contest.update_rule", "contest.update_schedule"},
    "participants_manager": {"contest.participant.view", "contest.participant.manage", "contest.participant.create", "contest.participant.update", "contest.participant.remove", "contest.participant.bulk_create", "contest.access_log.view"},
    "posts_manager": {"contest.board.question.view", "contest.board.question.manage", "contest.board.answer.create"},
    "notices_manager": {"contest.notice.view", "contest.notice.manage", "contest.notice.create", "contest.notice.update", "contest.notice.delete", "contest.notice.emergency_publish"},
    "audit_viewer": {"contest.audit.view", "contest.access_log.view"},
    "staff_manager": {"contest.staff.view", "contest.staff.manage"},
    "problems_viewer": {"contest.problem.view"},
    "submissions_viewer": {"contest.submission.view", "contest.submission.source.view"},
    "scoreboard_viewer": {"contest.scoreboard.view"},
    "scoreboard_manager": {"contest.scoreboard.view", "contest.scoreboard.manage", "contest.scoreboard.freeze", "contest.scoreboard.unfreeze", "contest.scoreboard.setting"},
    "problem_author": {"contest.problem.view", "contest.problem.manage", "contest.problem.review", "contest.problem.test", "contest.problem.create", "contest.problem.update", "contest.problem.delete", "contest.problem.reorder", "contest.problem.resource.view", "contest.problem.resource.manage", "contest.testcase.view", "contest.testcase.manage", "contest.generator.view", "contest.generator.manage"},
    "problem_reviewer": {"contest.problem.view", "contest.problem.review", "contest.problem.test"},
    "participant_preview": {"contest.view", "contest.participant.preview"},
}


def validate_roles(roles: list[str]) -> list[str]:
    if not roles or any(role not in ROLE_PERMISSIONS for role in roles):
        raise ValueError("하나 이상의 올바른 권한을 선택해야 합니다.")
    roles = list(dict.fromkeys(roles))
    if any(role in {"owner", "master"} for role in roles) and len(roles) != 1:
        raise ValueError("대회 총괄과 마스터는 각각 단독 권한입니다.")
    return roles


def permissions_for_roles(roles: list[str]) -> list[str]:
    roles = validate_roles(roles)
    if roles == ["owner"]:
        return ["contest.*", "contest.owner"]
    if roles == ["master"]:
        return ["contest.*"]
    return sorted({"contest.view"}.union(*(ROLE_PERMISSIONS[role] for role in roles)))


def roles_for_scopes(scopes: list[str]) -> list[str]:
    """Compatibility for accounts created before roles were persisted."""
    if "contest.owner" in scopes:
        return ["owner"]
    if "contest.*" in scopes:
        return ["master"]
    permissions_set = set(scopes)
    if "contest.participant.preview" in permissions_set:
        permissions_set.add("contest.view")
    if "contest.problem.review" in permissions_set:
        permissions_set.add("contest.problem.view")
    return [role for role, permissions in ROLE_PERMISSIONS.items() if permissions <= permissions_set]


def title_for_roles(roles: list[str]) -> str | None:
    if "owner" in roles:
        return "총괄"
    if "master" in roles:
        return "마스터"
    if "problem_author" in roles:
        return "출제진"
    if "problem_reviewer" in roles:
        return "검수진"
    return "운영진" if any(role in ROLE_PERMISSIONS for role in roles) else None


def title_for_scopes(scopes: list[str]) -> str | None:
    if "contest.owner" in scopes:
        return "총괄"
    if any(scope in {"*", "master", "contest.*"} for scope in scopes):
        return "마스터"
    author_scopes = {"contest.problem", "contest.problem.*", "contest.problem.manage", "contest.problem.create", "contest.problem.update", "contest.problem.delete", "contest.problem.reorder"}
    author_groups = ("contest.problem.resource", "contest.testcase", "contest.generator")
    if any(scope in author_scopes or any(scope == group or scope.startswith(group + ".") for group in author_groups) for scope in scopes):
        return "출제진"
    if any(scope in {"contest.problem.review", "contest.problem.test"} for scope in scopes):
        return "검수진"
    non_operational = {"contest.view", "contest.problem.review", "contest.problem.test"}
    if any(scope.startswith("contest.") and scope not in non_operational for scope in scopes):
        return "운영진"
    return "운영진" if "contest.view" in scopes else None


class ContestOperatorUpdateRequest(BaseModel):
    email: EmailStr | None = None
    display_name: str = Field(min_length=1, max_length=120)
    roles: list[str] = Field(min_length=1, max_length=len(ROLE_PERMISSIONS))

    @field_validator("email", mode="before")
    @classmethod
    def normalized_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("이름을 입력해야 합니다.")
        return value.strip()

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        if "owner" in value:
            raise ValueError("대회 총괄은 권한 위임으로만 변경할 수 있습니다.")
        return validate_roles(value)


class ContestOperatorCreateRequest(ContestOperatorUpdateRequest):
    email: EmailStr
