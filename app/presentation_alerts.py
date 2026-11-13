"""Presentation warning preferences, independent from fixed countdown timing."""
from typing import Annotated

from pydantic import BeforeValidator


DEFAULT_ALERT_MINUTES = (30, 20, 10, 5)
PRESENTATION_ALERT_FIELDS = (
    "presentation_freeze_alert_minutes",
    "presentation_end_alert_minutes",
)


def default_alert_minutes() -> list[int]:
    return list(DEFAULT_ALERT_MINUTES)


def normalize_alert_minutes(value) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("알림 시각은 분 단위 숫자 목록으로 입력해 주세요. 알림을 끄려면 빈 목록을 사용해 주세요.")
    if len(value) > 32:
        raise ValueError("알림 시각은 최대 32개까지 설정할 수 있습니다.")
    if any(type(minute) is not int or not 1 <= minute <= 10080 for minute in value):
        raise ValueError("알림 시각은 1분부터 10080분(7일) 사이의 정수로 입력해 주세요.")
    return sorted(set(value), reverse=True)


AlertMinutes = Annotated[list[int], BeforeValidator(normalize_alert_minutes)]
