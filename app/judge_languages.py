"""Canonical submission languages and per-problem admission policy."""
from pydantic import BaseModel, Field, field_validator
from app.services.errors import AppError

DEFAULT_LANGUAGES = ("cpp17", "python311", "java8", "c99", "pypy3", "kotlin")
LANGUAGES = (*DEFAULT_LANGUAGES, "python313")
NEW_LANGUAGES = {"python311", "pypy3", "kotlin"}
RUNTIME_CAPABILITY = "judge-zoj-runtime-v1"


def validate_languages(value):
    if not isinstance(value, list) or not value or len(value) > len(LANGUAGES) or any(item not in LANGUAGES for item in value) or len(set(value)) != len(value):
        raise ValueError("제출 가능한 언어를 중복 없이 한 개 이상 선택하세요.")
    # Imported archives and older clients may still use the previous default ID.
    # Admission uses the current runtime; stored submissions retain their own ID.
    return list(dict.fromkeys("python311" if item == "python313" else item for item in value))


class ProgrammingLanguages(BaseModel):
    allowed_languages: list[str] = Field(default_factory=lambda: list(DEFAULT_LANGUAGES))

    @field_validator("allowed_languages", mode="before")
    @classmethod
    def languages(cls, value):
        return validate_languages(value)


def require_allowed(problem, language):
    if language not in LANGUAGES:
        raise AppError(422, "unsupported_language", "지원하지 않는 제출 언어입니다.")
    if language not in problem.allowed_languages:
        raise AppError(422, "language_not_allowed", "이 문제에서 허용하지 않는 제출 언어입니다.")
