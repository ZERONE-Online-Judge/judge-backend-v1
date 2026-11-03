"""Read account-wide Resend counters without retrieving individual emails."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from time import monotonic

import httpx

from app.settings import settings

SUCCESS_TTL_SECONDS = 60
ERROR_TTL_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 4.0

# One entry bounds memory even when credentials/configuration rotate. The lock
# also makes concurrent refreshes share a single pair of upstream requests.
_cache_lock = Lock()
_cache_key: tuple[str, str, str] | None = None
_cache_value: dict | None = None
_cache_expires_at = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _iso(parsed) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _count(value) -> bool:
    # bool is an int subclass, but is not an email counter.
    return type(value) is int and value >= 0


def _quota(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    if not all(_count(value.get(field)) for field in ("sent", "received", "used")):
        return None
    if "limit" not in value or (value["limit"] is not None and not _count(value["limit"])):
        return None
    return {
        "sent": value["sent"],
        "received": value["received"],
        "used": value["used"],
        "limit": value["limit"],
        "resets_at": _timestamp(value.get("resets_at")),
    }


def _empty(provider: str, now: datetime, status: str, message: str | None) -> dict:
    return {
        "provider": provider,
        "generated_at": _iso(now),
        "quota": {"status": status, "daily": None, "monthly": None, "message": message},
        "weekly": {"status": status, "sent": None, "start_at": None, "end_at": None, "message": message},
        "metrics_delay_minutes": 15,
    }


def _get(client: httpx.Client, path: str, *, params=None) -> tuple[dict | None, str, str | None]:
    try:
        response = client.get(path, params=params)
        if response.status_code in (401, 403):
            return None, "error", "발송량 조회에는 전체 접근 권한이 있는 Resend API 키가 필요합니다."
        if response.status_code == 429:
            return None, "error", "Resend 조회 요청이 많습니다. 잠시 후 다시 확인해 주세요."
        if response.status_code in (404, 405, 501):
            return None, "unsupported", "현재 Resend API에서는 이 발송량 조회를 지원하지 않습니다."
        if not 200 <= response.status_code < 300:
            return None, "error", "Resend 발송량을 조회하지 못했습니다. 잠시 후 다시 확인해 주세요."
        body = response.json()
        if not isinstance(body, dict):
            return None, "error", "Resend 발송량 응답을 확인할 수 없습니다."
        return body, "ok", None
    except httpx.TimeoutException:
        return None, "error", "Resend 발송량 조회 시간이 초과되었습니다. 잠시 후 다시 확인해 주세요."
    except (httpx.HTTPError, ValueError):
        # Never reflect provider bodies, exceptions (which can include URLs),
        # authorization headers or individual email information to the client.
        return None, "error", "Resend 발송량을 조회하지 못했습니다. 잠시 후 다시 확인해 주세요."


def _fetch(provider: str, base_url: str, api_key: str, now: datetime) -> dict:
    result = _empty(provider, now, "error", None)
    try:
        with httpx.Client(
            base_url=base_url + "/",
            headers={"Authorization": "Bearer " + api_key},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            body, status, message = _get(client, "usage")
            if body is not None:
                emails = body.get("emails")
                daily = _quota(emails.get("daily")) if isinstance(emails, dict) else None
                monthly = _quota(emails.get("monthly")) if isinstance(emails, dict) else None
                valid = daily is not None and monthly is not None
                result["quota"] = {
                    "status": "ok" if valid else "error",
                    "daily": daily,
                    "monthly": monthly,
                    "message": None if valid else "Resend의 일부 사용량 또는 한도를 확인할 수 없습니다.",
                }
            else:
                result["quota"].update(status=status, message=message)

            body, status, message = _get(client, "emails/metrics", params={
                "metrics": "sent",
                "start_date": _iso(now - timedelta(days=7)),
                "end_date": _iso(now),
                "timezone": "Asia/Seoul",
            })
            if body is not None:
                totals = body.get("totals")
                sent = totals.get("sent") if isinstance(totals, dict) else None
                start_at, end_at = _timestamp(body.get("start_date")), _timestamp(body.get("end_date"))
                valid = (
                    _count(sent) and start_at is not None and end_at is not None
                    and datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                    <= datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                )
                result["weekly"] = {
                    "status": "ok" if valid else "error",
                    "sent": sent if valid else None,
                    "start_at": start_at if valid else None,
                    "end_at": end_at if valid else None,
                    "message": None if valid else "Resend의 최근 7일 발송량 또는 조회 기간을 확인할 수 없습니다.",
                }
            else:
                result["weekly"].update(status=status, message=message)
    except (httpx.HTTPError, ValueError):
        message = "Resend 연결 설정을 확인해 주세요."
        for section in ("quota", "weekly"):
            result[section].update(status="error", message=message)
    return result


def mail_usage() -> dict:
    """Return cached provider counters; callers must authenticate first."""
    global _cache_key, _cache_value, _cache_expires_at
    provider = settings.mail_delivery_provider.strip().lower()
    base_url = settings.resend_api_url.rstrip("/")
    # Match the existing mail worker's legacy endpoint compatibility.
    if base_url.endswith("/emails"):
        base_url = base_url.removesuffix("/emails")
    api_key = (settings.resend_api_key or "").strip()
    config_key = (provider, base_url, sha256(api_key.encode()).hexdigest())
    with _cache_lock:
        if config_key == _cache_key and _cache_value is not None and monotonic() < _cache_expires_at:
            return deepcopy(_cache_value)
        now = _now()
        if provider != "resend":
            result = _empty(provider, now, "unsupported", "현재 메일 발송 서비스는 Resend가 아닙니다.")
        elif not api_key:
            result = _empty(provider, now, "not_configured", "Resend API 키가 설정되지 않았습니다.")
        else:
            result = _fetch(provider, base_url, api_key, now)
        complete = all(result[section]["status"] == "ok" for section in ("quota", "weekly"))
        _cache_key, _cache_value = config_key, result
        _cache_expires_at = monotonic() + (SUCCESS_TTL_SECONDS if complete else ERROR_TTL_SECONDS)
        return deepcopy(result)
