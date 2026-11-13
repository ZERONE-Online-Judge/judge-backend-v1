"""Bounded internal timing evidence; absent values remain absent for old agents."""
from typing import Literal

from pydantic import BaseModel, Field

TimeoutOrigin = Literal["cpu", "wall", "watchdog", "unknown"]


class TimingEvidence(BaseModel):
    timeout_origin: TimeoutOrigin | None = None
    retry_reason: TimeoutOrigin | None = None
    cpu_runtime_ms: int | None = Field(default=None, ge=0, le=86400000, strict=True)
    wall_runtime_ms: int | None = Field(default=None, ge=0, le=86400000, strict=True)
    cpu_limit_ms: int | None = Field(default=None, ge=0, le=86400000, strict=True)
    wall_limit_ms: int | None = Field(default=None, ge=0, le=86400000, strict=True)
    attempts: int | None = Field(default=None, ge=1, le=2, strict=True)


TIMING_FIELDS = tuple(TimingEvidence.model_fields)


def timing_evidence(value):
    return TimingEvidence.model_validate(value).model_dump(exclude_none=True)
