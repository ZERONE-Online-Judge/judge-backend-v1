"""Read durable Editor jobs through a purpose-bound server bridge."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Literal
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx

from app.services.errors import AppError
from app.settings import settings

JobKind = Literal["all", "prepare", "verify", "validator-tests", "checker-tests", "stress", "package", "render"]
JobCategory = Literal["all", "prepare", "prepare-full", "prepare-inputs", "prepare-validate", "prepare-answers",
                      "verify", "verify-single", "validator-tests", "checker-tests", "stress", "package", "render"]
JobStatus = Literal["all", "queued", "running", "completed", "failed", "cancelled"]

PURPOSE = "zoj-editor-admin-history-v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMEOUT = httpx.Timeout(15.0, connect=3.0, pool=3.0)
TOTAL_TIMEOUT_SECONDS = 20.0
JOB_FIELDS = {"id", "problem_id", "problem", "requested_by", "requester", "kind", "category", "status", "name",
              "outcome", "revision", "workspace_version", "created_at", "started_at", "finished_at", "result",
              "options", "result_redacted", "result_truncated"}
SUMMARY_FIELDS = ("total", "queued", "running", "completed", "failed", "cancelled")


def _target():
    base = settings.editor_api_url.rstrip("/")
    try:
        parsed = urlsplit(base)
    except ValueError:
        raise AppError(503, "editor_history_unavailable", "Editor 작업 내역 연결이 아직 설정되지 않았습니다.") from None
    secret = settings.editor_bridge_secret.get_secret_value() if settings.editor_bridge_secret else ""
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/api" or len(secret) < 32
            or (settings.app_env == "production" and parsed.scheme != "https")):
        raise AppError(503, "editor_history_unavailable", "Editor 작업 내역 연결이 아직 설정되지 않았습니다.")
    return base, secret


def _headers(email, path, secret):
    encoded = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")
    stamp, nonce = str(int(time.time())), uuid4().hex
    digest = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join([PURPOSE, "GET", path, stamp, nonce, encoded, digest])
    return {"X-Editor-Identity": encoded, "X-Editor-Timestamp": stamp, "X-Editor-Nonce": nonce,
            "X-Editor-Content-Sha256": digest,
            "X-Editor-Signature": hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest(),
            "Accept": "application/json"}


def _invalid():
    return AppError(502, "editor_history_invalid", "Editor 작업 내역의 응답을 확인하지 못했습니다.")


def _job(value, expected_id=None):
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or (expected_id is not None and value["id"] != expected_id)
            or not isinstance(value.get("problem"), dict) or not isinstance(value.get("requester"), dict)
            or not isinstance(value.get("result"), dict) or not isinstance(value.get("outcome"), dict)):
        raise _invalid()
    # Editor projects the result and options before returning them. Keep the
    # public top-level contract narrow even if its internal API later grows.
    return {key: item for key, item in value.items() if key in JOB_FIELDS}


def _payload(body, job_id=None, page_size=20):
    if job_id is not None:
        return _job(body, job_id)
    if (not isinstance(body, dict) or not isinstance(body.get("items"), list)
            or len(body["items"]) > page_size
            or any(type(body.get(key)) is not int for key in ("total", "page", "page_size", "pages"))
            or body["total"] < 0 or body["pages"] < 1 or not 1 <= body["page"] <= body["pages"]
            or body["page_size"] != page_size):
        raise _invalid()
    result = {key: body[key] for key in ("total", "page", "page_size", "pages")} | {
        "items": [_job(item) for item in body["items"]]}
    if "summary" in body:
        summary = body["summary"]
        if not isinstance(summary, dict) or any(type(summary.get(key)) is not int or summary[key] < 0 for key in SUMMARY_FIELDS):
            raise _invalid()
        result["summary"] = {key: summary[key] for key in SUMMARY_FIELDS}
    return result


async def read(email, *, job_id=None, page=1, page_size=20, q="", kind="all", category="all", status="all"):
    base, secret = _target()
    path = "/api/internal/admin/jobs" + ("/" + job_id if job_id else "")
    if job_id is None:
        path += "?" + urlencode({"page": page, "page_size": page_size, "q": q.strip(),
                                  "kind": kind, "category": category, "status": status})
    url = base.removesuffix("/api") + path
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS), httpx.AsyncClient(
                verify=settings.editor_ca_file or True, timeout=TIMEOUT,
                follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers=_headers(email, path, secret)) as response:
                if response.status_code == 404 and job_id is not None:
                    raise AppError(404, "editor_job_not_found", "Editor 실행 기록을 찾을 수 없습니다.")
                if response.status_code == 429:
                    raise AppError(503, "editor_history_busy", "Editor 작업 내역 조회가 많습니다. 잠시 후 다시 확인해 주세요.")
                if response.status_code != 200:
                    raise AppError(502, "editor_history_unavailable", "Editor 작업 내역을 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _invalid()
                    content.extend(chunk)
        body = json.loads(content)
    except (httpx.TimeoutException, TimeoutError):
        raise AppError(504, "editor_history_timeout", "Editor 작업 내역 조회 시간이 초과되었습니다. 다시 확인해 주세요.") from None
    except (httpx.HTTPError, OSError):
        raise AppError(502, "editor_history_unavailable", "Editor 작업 내역에 연결하지 못했습니다. 잠시 후 다시 확인해 주세요.") from None
    except (ValueError, UnicodeError):
        raise _invalid() from None
    return _payload(body, job_id=job_id, page_size=page_size)
