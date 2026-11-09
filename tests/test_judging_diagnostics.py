"""Only safe correlation fields from judging input failures reach logs."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.main import app_error_handler
from app.services.errors import AppError


@pytest.mark.parametrize("code,reason", [
    ("testcases_not_ready", "no_active_testcases"),
    ("testcase_results_invalid", "aggregate_mismatch"),
])
def test_judging_log_contains_only_fixed_code_reason_and_request_id(caplog, code, reason):
    request_id = "req_" + "a" * 32
    request = SimpleNamespace(state=SimpleNamespace(request_id=request_id))
    error = AppError(422, code, "PRIVATE exception body and source code",
        {"reason": reason, "node_secret": "PRIVATE_SECRET", "lease_token": "PRIVATE_LEASE",
         "source_code": "PRIVATE_SOURCE", "stdout": "PRIVATE_OUTPUT"})
    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = asyncio.run(app_error_handler(request, error))
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == (
        f"judging_request_rejected code={code} reason={reason} request_id={request_id}")
    assert "PRIVATE" not in caplog.text
    # Logging does not change the existing response contract.
    body = json.loads(response.body)
    assert body["error"]["request_id"] == request_id
    assert body["error"]["details"] == error.details


def test_unknown_reason_and_injected_request_id_are_not_logged(caplog):
    request = SimpleNamespace(state=SimpleNamespace(request_id="PRIVATE\nforged-log"))
    error = AppError(422, "testcase_results_invalid", "PRIVATE", {"reason": "PRIVATE_REASON"})
    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(app_error_handler(request, error))
    assert caplog.records[0].getMessage() == (
        "judging_request_rejected code=testcase_results_invalid reason=invalid_evidence request_id=unavailable")
    assert "PRIVATE" not in caplog.text and "forged-log" not in caplog.text


def test_unrelated_app_errors_do_not_use_judging_diagnostic_log(caplog):
    request = SimpleNamespace(state=SimpleNamespace(request_id="req_" + "b" * 32))
    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(app_error_handler(request, AppError(409, "lease_conflict", "Lease token mismatch.")))
    assert caplog.records == []
