"""Only aggregate, authenticated, bounded reads are used for mail usage."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier, Lock
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.routers.admin import router
from app.services import resend_usage
from app.services.errors import AppError
from app.services.store import store
from app.settings import settings

NOW = datetime(2026, 11, 3, 8, 30, 0, tzinfo=timezone.utc)
KEY = "re_PRIVATE_KEY_NOT_FOR_CLIENTS"
USAGE = {
    "object": "usage",
    "emails": {
        "daily": {"sent": 8, "received": 2, "used": 10, "limit": 100,
                  "resets_at": "2026-11-03T23:59:59.999Z"},
        "monthly": {"sent": 120, "received": 4, "used": 124, "limit": 3000,
                    "resets_at": "2026-11-27T13:56:24.456Z"},
    },
}
METRICS = {"object": "metrics", "totals": {"sent": 53},
           "start_date": "2026-10-27T08:30:00.000Z", "end_date": "2026-11-03T08:30:00.000Z"}
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "mail_delivery_provider", "resend")
    monkeypatch.setattr(settings, "resend_api_key", KEY)
    monkeypatch.setattr(settings, "resend_api_url", "https://resend.test")
    monkeypatch.setattr(resend_usage, "_cache_key", None)
    monkeypatch.setattr(resend_usage, "_cache_value", None)
    monkeypatch.setattr(resend_usage, "_cache_expires_at", 0)
    monkeypatch.setattr(resend_usage, "_now", lambda: NOW)


@pytest.fixture
def upstream(monkeypatch):
    calls = []

    def install(usage=None, metrics=None, handler=None):
        def respond(request):
            calls.append(request)
            assert request.method == "GET"
            assert request.url.path in {"/usage", "/emails/metrics"}
            assert request.headers["Authorization"] == "Bearer " + settings.resend_api_key
            if handler:
                return handler(request)
            value = (USAGE if usage is None else usage) if request.url.path == "/usage" else (METRICS if metrics is None else metrics)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json=value)
        transport = httpx.MockTransport(respond)
        monkeypatch.setattr(resend_usage.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))
        return calls
    return install


def test_real_periods_counts_and_seven_day_request(upstream):
    calls = upstream()
    result = resend_usage.mail_usage()
    assert result["provider"] == "resend" and result["metrics_delay_minutes"] == 15
    assert result["generated_at"] == "2026-11-03T08:30:00Z"
    quota = result["quota"]
    assert quota["status"] == "ok" and quota["message"] is None
    assert (quota["daily"]["sent"], quota["daily"]["used"], quota["daily"]["received"]) == (8, 10, 2)
    assert quota["monthly"]["sent"] == 120
    # Billing reset comes from the provider, not a calendar-month assumption.
    assert quota["monthly"]["resets_at"] == "2026-11-27T13:56:24.456000Z"
    weekly = result["weekly"]
    assert weekly == {"status": "ok", "sent": 53, "start_at": "2026-10-27T08:30:00Z",
                      "end_at": "2026-11-03T08:30:00Z", "message": None}
    assert len(calls) == 2
    assert dict(calls[1].url.params) == {
        "metrics": "sent", "start_date": "2026-10-27T08:30:00Z",
        "end_date": "2026-11-03T08:30:00Z", "timezone": "Asia/Seoul",
    }
    assert "limit" not in weekly  # A week is a statistic, not a Resend quota.


def test_null_means_unlimited_and_zero_is_real_data(upstream):
    body = deepcopy(USAGE)
    body["emails"]["daily"].update(sent=0, received=0, used=0, limit=None)
    upstream(usage=body, metrics={**METRICS, "totals": {"sent": 0}})
    result = resend_usage.mail_usage()
    assert result["quota"]["status"] == "ok"
    assert result["quota"]["daily"]["limit"] is None
    assert result["quota"]["daily"]["sent"] == 0
    assert result["weekly"]["status"] == "ok" and result["weekly"]["sent"] == 0


def test_legacy_send_endpoint_configuration_matches_mail_worker(upstream, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_url", "https://resend.test/emails/")
    calls = upstream()
    assert resend_usage.mail_usage()["quota"]["status"] == "ok"
    assert [request.url.path for request in calls] == ["/usage", "/emails/metrics"]


@pytest.mark.parametrize("field,value", [("sent", "8"), ("received", -1), ("used", True), ("limit", "100"), ("limit", -2)])
def test_invalid_period_does_not_become_zero_or_invent_a_limit(upstream, field, value):
    body = deepcopy(USAGE)
    body["emails"]["daily"][field] = value
    upstream(usage=body)
    result = resend_usage.mail_usage()
    assert result["quota"]["status"] == "error" and result["quota"]["daily"] is None
    assert result["quota"]["monthly"]["limit"] == 3000
    assert result["weekly"]["status"] == "ok"


@pytest.mark.parametrize("field", ["sent", "received", "used", "limit"])
def test_missing_quota_field_is_unknown(upstream, field):
    body = deepcopy(USAGE)
    del body["emails"]["monthly"][field]
    upstream(usage=body)
    result = resend_usage.mail_usage()
    assert result["quota"]["monthly"] is None and result["quota"]["daily"]["limit"] == 100


@pytest.mark.parametrize("value", [None, "not-a-date", "2026-11-03T10:00:00"])
def test_missing_or_invalid_reset_stays_unknown(upstream, value):
    body = deepcopy(USAGE)
    body["emails"]["daily"]["resets_at"] = value
    upstream(usage=body)
    result = resend_usage.mail_usage()
    assert result["quota"]["daily"]["resets_at"] is None
    assert result["quota"]["daily"]["sent"] == 8


def test_provider_retention_clamp_is_displayed_instead_of_claiming_full_week(upstream):
    upstream(metrics={**METRICS, "start_date": "2026-10-30T08:30:00.000Z"})
    result = resend_usage.mail_usage()
    assert result["weekly"]["start_at"] == "2026-10-30T08:30:00Z"


@pytest.mark.parametrize("metrics", [
    {}, {**METRICS, "totals": {}}, {**METRICS, "totals": {"sent": -1}},
    {**METRICS, "totals": {"sent": "53"}}, {**METRICS, "totals": {"sent": True}},
    {**METRICS, "start_date": None}, {**METRICS, "end_date": "invalid"},
    {**METRICS, "start_date": "2026-11-04T00:00:00Z"},
])
def test_malformed_metrics_are_unknown_but_quota_survives(upstream, metrics):
    upstream(metrics=metrics)
    result = resend_usage.mail_usage()
    assert result["weekly"]["status"] == "error" and result["weekly"]["sent"] is None
    assert result["quota"]["status"] == "ok"


@pytest.mark.parametrize("value,expected", [
    (httpx.Response(403, json={"message": KEY + " recipient@example.com"}), "전체 접근"),
    (httpx.Response(401, json={"message": KEY}), "전체 접근"),
    (httpx.Response(429, json={"message": KEY}), "잠시 후"),
    (httpx.ReadTimeout(KEY), "시간이 초과"),
    (httpx.Response(200, content=b"not-json"), "조회하지 못"),
    (httpx.Response(200, json=[]), "응답을 확인"),
    (httpx.Response(500, content=KEY.encode()), "조회하지 못"),
])
def test_upstream_failures_are_sanitized_and_partial(upstream, value, expected):
    upstream(usage=value)
    result = resend_usage.mail_usage()
    assert result["quota"]["status"] == "error" and result["quota"]["daily"] is None
    assert expected in result["quota"]["message"]
    assert result["weekly"]["status"] == "ok"
    assert KEY not in str(result) and "recipient@example.com" not in str(result)


def test_unsupported_upstream_and_unconfigured_provider_do_not_invent_values(upstream, monkeypatch):
    calls = upstream(usage=httpx.Response(404))
    assert resend_usage.mail_usage()["quota"]["status"] == "unsupported"
    monkeypatch.setattr(settings, "resend_api_key", None)
    result = resend_usage.mail_usage()
    assert result["quota"]["status"] == result["weekly"]["status"] == "not_configured"
    monkeypatch.setattr(settings, "mail_delivery_provider", "smtp")
    result = resend_usage.mail_usage()
    assert result["quota"]["status"] == result["weekly"]["status"] == "unsupported"
    assert len(calls) == 2


def test_cache_is_bounded_config_sensitive_and_not_client_mutable(upstream, monkeypatch):
    clock = [50.0]
    monkeypatch.setattr(resend_usage, "monotonic", lambda: clock[0])
    calls = upstream()
    result = resend_usage.mail_usage()
    result["quota"]["daily"]["sent"] = 999
    assert resend_usage.mail_usage()["quota"]["daily"]["sent"] == 8
    assert len(calls) == 2
    clock[0] += 59.9
    resend_usage.mail_usage()
    assert len(calls) == 2
    clock[0] += 0.2
    resend_usage.mail_usage()
    assert len(calls) == 4
    monkeypatch.setattr(settings, "resend_api_key", KEY + "2")
    resend_usage.mail_usage()
    assert len(calls) == 6
    monkeypatch.setattr(settings, "resend_api_url", "https://other-resend.test/api")
    # Use a handler accepting the explicit changed base path.
    monkeypatch.setattr(resend_usage.httpx, "Client", lambda **kwargs: REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=USAGE if request.url.path.endswith("/usage") else METRICS)),
        **kwargs))
    resend_usage.mail_usage()
    assert resend_usage._cache_key[:2] == ("resend", "https://other-resend.test/api")
    assert KEY not in str(resend_usage._cache_key)


def test_failure_cache_retries_after_ten_seconds(upstream, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(resend_usage, "monotonic", lambda: clock[0])
    calls = upstream(usage=httpx.Response(429))
    resend_usage.mail_usage()
    clock[0] = 9.9
    resend_usage.mail_usage()
    assert len(calls) == 2
    clock[0] = 10.0
    resend_usage.mail_usage()
    assert len(calls) == 4


def test_concurrent_refreshes_share_one_upstream_read(upstream):
    barrier = Barrier(6)
    calls_lock = Lock()
    calls = []
    def handler(request):
        with calls_lock:
            calls.append(request.url.path)
        return httpx.Response(200, json=USAGE if request.url.path == "/usage" else METRICS)
    upstream(handler=handler)
    def refresh(_):
        barrier.wait(timeout=3)
        return resend_usage.mail_usage()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(refresh, range(6)))
    assert calls == ["/usage", "/emails/metrics"]
    assert all(result == results[0] for result in results)


def test_route_authorizes_before_provider_and_prevents_browser_cache(upstream, monkeypatch):
    # Keep the real authz check; isolated tokens avoid unrelated login/email work.
    monkeypatch.setattr(store, "get_staff_by_access_token", lambda token:
                        SimpleNamespace(is_service_master=token == "master") if token in {"master", "operator"} else None)
    monkeypatch.setattr(store, "get_staff_by_general_access_token", lambda token: None)
    app = FastAPI()
    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "mail-usage-test"
        return await call_next(request)
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    calls = upstream()
    assert client.get("/api/admin/mail-usage").status_code == 401
    assert client.get("/api/admin/mail-usage", headers={"Authorization": "Bearer operator"}).status_code == 403
    assert calls == []
    response = client.get("/api/admin/mail-usage", headers={"Authorization": "Bearer master"})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["request_id"] == "mail-usage-test"
    assert response.json()["data"]["weekly"]["sent"] == 53
    client.get("/api/admin/mail-usage", headers={"Authorization": "Bearer master"}, params={"refresh": "true"})
    assert len(calls) == 2
