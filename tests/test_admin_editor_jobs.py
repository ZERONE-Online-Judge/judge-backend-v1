"""Only service masters can make bounded, signed reads of durable Editor jobs."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.routers.admin import router
from app.services import admin_editor_jobs as history
from app.services.errors import AppError
from app.services.store import store
from app.settings import settings

KEY = "history-test-secret-at-least-32-characters"
MASTER = "master@example.com"
JOB_ID = "ab" * 16
REAL_CLIENT = httpx.AsyncClient
JOB = {"id": JOB_ID, "problem": {"id": "cd" * 16, "name": "문제", "slug": "sample", "deleted": False},
       "requester": {"id": "ef" * 16, "display_name": "출제자", "handle": "author", "email": "author@example.com"},
       "kind": "verify", "category": "verify-single", "status": "completed", "name": "단일 검수",
       "outcome": {"execution": "completed", "assessment": {"status": "passed", "code": "expectations_match"}},
       "revision": 2, "workspace_version": 4, "created_at": 1790000000, "started_at": 1790000001,
       "finished_at": 1790000002, "result": {"summary": "검증 완료", "progress": 100}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "editor_bridge_secret", SecretStr(KEY))
    monkeypatch.setattr(settings, "editor_api_url", "https://editor.test/api")
    monkeypatch.setattr(settings, "editor_ca_file", None)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(store, "get_staff_by_access_token", lambda token:
        SimpleNamespace(is_service_master=token == "master", email=MASTER) if token in {"master", "operator"} else None)
    monkeypatch.setattr(store, "get_staff_by_general_access_token", lambda token: None)
    app = FastAPI()
    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "history-test"
        return await call_next(request)
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})
    app.include_router(router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def upstream(monkeypatch):
    calls, client_options = [], []
    def install(value=None, handler=None):
        def respond(request):
            calls.append(request)
            if handler:
                return handler(request)
            body = value if value is not None else {"items": [JOB], "page": 1, "page_size": 20, "pages": 1, "total": 1}
            return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)
        def create(**kwargs):
            client_options.append(kwargs)
            return REAL_CLIENT(transport=httpx.MockTransport(respond), **kwargs)
        monkeypatch.setattr(history.httpx, "AsyncClient", create)
        return calls, client_options
    return install


def get(client, path="", token="master", **params):
    return client.get("/api/admin/judge/editor-jobs" + path, params=params,
                      headers={"Authorization": "Bearer " + token, "Cookie": "private_session=never-forward"})


def test_master_only_before_network_and_read_only(client, upstream):
    calls, _ = upstream()
    for path in ["", "/" + JOB_ID]:
        assert get(client, path, token="unknown").status_code == 401
        assert get(client, path, token="operator").status_code == 403
    assert calls == []
    for method in ["post", "patch", "delete"]:
        assert getattr(client, method)("/api/admin/judge/editor-jobs/" + JOB_ID).status_code == 405
    assert calls == []


def test_paging_filters_query_and_identity_are_signed_exactly(client, upstream):
    payload = {"items": [JOB], "total": 23, "page": 2, "pages": 23, "page_size": 1}
    calls, options = upstream(payload)
    response = get(client, page=2, page_size=1, q="  문제 + & / ?  ", kind="verify", category="verify-single", status="completed")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"data": payload, "request_id": "history-test"}
    request = calls[0]
    assert request.method == "GET" and request.url.host == "editor.test"
    assert request.url.params == httpx.QueryParams(page=2, page_size=1, q="문제 + & / ?", kind="verify", category="verify-single", status="completed")
    assert request.content == b"" and "authorization" not in request.headers and "cookie" not in request.headers
    headers = request.headers
    assert base64.urlsafe_b64decode(headers["x-editor-identity"] + "==").decode() == MASTER
    assert len(headers["x-editor-nonce"]) == 32
    canonical = "\n".join([history.PURPOSE, "GET", request.url.raw_path.decode("ascii"),
        headers["x-editor-timestamp"], headers["x-editor-nonce"], headers["x-editor-identity"], hashlib.sha256(b"").hexdigest()])
    expected = hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert headers["x-editor-signature"] == expected
    tampered = canonical.replace("page=2", "page=1")
    assert hmac.new(KEY.encode(), tampered.encode(), hashlib.sha256).hexdigest() != expected
    assert options[0]["verify"] is True and options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["timeout"].connect == 3 and options[0]["timeout"].read == 15
    get(client, page=2, page_size=1)
    assert calls[0].headers["x-editor-nonce"] != calls[1].headers["x-editor-nonce"]


def test_detail_preserves_projected_results_and_ignores_unknown_top_level_fields(client, upstream):
    payload = {**deepcopy(JOB), "options": {"solutions": ["Main.java"], "snapshot": "committed"},
               "result": {"summary": "실패", "progress": 100, "solutions": [{"name": "Main.java", "matched": False}]},
               "result_redacted": True, "result_truncated": False,
               "document": {"secret": "must-not-escape"}, "lease": "must-not-escape"}
    calls, _ = upstream(payload)
    response = get(client, "/" + JOB_ID)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"] == payload["result"]
    assert response.json()["data"]["options"] == payload["options"]
    assert response.json()["data"]["result_redacted"] is True
    assert response.json()["data"]["result_truncated"] is False
    assert "must-not-escape" not in response.text
    assert calls[0].url.raw_path.decode() == "/api/internal/admin/jobs/" + JOB_ID
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("params", [{"page": 0}, {"page": 1000001}, {"page_size": 0}, {"page_size": 101},
    {"q": "x" * 201}, {"kind": "unknown"}, {"category": "other"}, {"status": "preparing"}])
def test_rejects_invalid_filters_before_upstream(client, upstream, params):
    calls, _ = upstream()
    assert get(client, **params).status_code == 422
    assert calls == []


@pytest.mark.parametrize("job_id", ["..", "123", "A" * 32, "a" * 33, "not-a-job"])
def test_job_path_is_not_arbitrary(client, upstream, job_id):
    calls, _ = upstream()
    # HTTP clients normalize dot segments before a path reaches FastAPI.
    assert get(client, "/" + job_id).status_code == (404 if job_id == ".." else 422)
    assert calls == []


@pytest.mark.parametrize("url", ["", "https://editor.test/wrong", "https://user:pass@editor.test/api", "https://editor.test/api?q=1",
    "https://editor.test/api#secret", "http://editor.test/api", "https://[invalid/api"])
def test_disabled_or_unsafe_configuration_is_not_a_request_target(client, upstream, monkeypatch, url):
    calls, _ = upstream()
    monkeypatch.setattr(settings, "editor_api_url", url)
    response = get(client)
    assert response.status_code == 503
    assert calls == []
    if url:
        assert url not in response.json()["error"]["message"]


def test_secret_required_and_custom_ca_preserved(client, upstream, monkeypatch):
    calls, options = upstream()
    monkeypatch.setattr(settings, "editor_bridge_secret", SecretStr("short"))
    assert get(client).status_code == 503 and calls == []
    monkeypatch.setattr(settings, "editor_bridge_secret", SecretStr(KEY))
    monkeypatch.setattr(settings, "editor_ca_file", "/configured/ca.pem")
    assert get(client).status_code == 200
    assert options[0]["verify"] == "/configured/ca.pem"


@pytest.mark.parametrize("status,expected", [(301, 502), (401, 502), (403, 502), (404, 502), (429, 503), (500, 502), (503, 502)])
def test_upstream_errors_are_sanitized_without_auth_downgrade(client, upstream, status, expected):
    calls, _ = upstream(httpx.Response(status, content=b"private_token secret document", headers={"Location": "https://attacker.test"}))
    response = get(client)
    assert response.status_code == expected
    assert "private_token" not in response.text and "attacker" not in response.text and KEY not in response.text
    assert len(calls) == 1


def test_missing_detail_is_404(client, upstream):
    upstream(httpx.Response(404, content=b"private-error"))
    response = get(client, "/" + JOB_ID)
    assert response.status_code == 404 and "private-error" not in response.text


@pytest.mark.parametrize("error,expected", [(httpx.ReadTimeout("private_token"), 504), (httpx.ConnectError("private_token"), 502)])
def test_network_errors_are_sanitized(client, upstream, error, expected):
    def fail(request):
        raise error
    upstream(handler=fail)
    response = get(client)
    assert response.status_code == expected and "private_token" not in response.text


@pytest.mark.parametrize("body", [[], {"items": []}, {"items": [JOB] * 21, "page": 1, "page_size": 20, "pages": 2, "total": 21},
    {"items": [], "page": 0, "page_size": 20, "pages": 1, "total": 0},
    {"items": [], "page": 1, "page_size": 20, "pages": 1, "total": True},
    {"items": ["private-secret"], "page": 1, "page_size": 20, "pages": 1, "total": 1}])
def test_malformed_history_is_not_forwarded(client, upstream, body):
    upstream(body)
    response = get(client)
    assert response.status_code == 502 and "private-secret" not in response.text


def test_invalid_json_and_oversized_response_are_bounded(client, upstream, monkeypatch):
    upstream(httpx.Response(200, content=b"not-json private-secret"))
    assert get(client).status_code == 502
    monkeypatch.setattr(history, "MAX_RESPONSE_BYTES", 128)
    upstream(httpx.Response(200, content=json.dumps({"private": "x" * 129}).encode()))
    assert get(client).status_code == 502


def test_detail_must_match_requested_id(client, upstream):
    upstream({**JOB, "id": "00" * 16})
    assert get(client, "/" + JOB_ID).status_code == 502


def test_empty_page_and_clamped_pagination_keep_filtered_totals(client, upstream):
    summary = {"total": 23, "queued": 2, "running": 1, "completed": 18, "failed": 1, "cancelled": 1}
    upstream({"items": [JOB], "page": 3, "page_size": 10, "pages": 3, "total": 23, "summary": summary})
    response = get(client, page=999, page_size=10)
    assert response.status_code == 200 and response.json()["data"]["page"] == 3
    assert response.json()["data"]["summary"] == summary
    upstream({"items": [], "page": 1, "page_size": 20, "pages": 1, "total": 0,
              "summary": {key: 0 for key in summary}})
    response = get(client)
    assert response.status_code == 200 and response.json()["data"]["items"] == []
    assert response.json()["data"]["total"] == 0


def test_malformed_summary_is_not_forwarded(client, upstream):
    upstream({"items": [], "page": 1, "page_size": 20, "pages": 1, "total": 0,
              "summary": {key: True for key in history.SUMMARY_FIELDS}})
    assert get(client).status_code == 502


def test_slow_response_has_an_overall_deadline(client, upstream, monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b"{}"
    monkeypatch.setattr(history, "TOTAL_TIMEOUT_SECONDS", 0.01)
    upstream(httpx.Response(200, stream=SlowBody()))
    response = get(client)
    assert response.status_code == 504
