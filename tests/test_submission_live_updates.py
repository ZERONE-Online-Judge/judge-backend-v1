"""Live waits remain page-bounded and never publish staged results as official."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.models import SubmissionStatus, now_utc
from app.orm_models import RejudgingItemRow, SubmissionRow
from app.routers import operator, participant
from app.services import rejudging, submission_status
from app.services.errors import AppError
from app.services.store import store
from tests.test_judge_security import database, leased, client, SECRET
from tests.test_staged_rejudging import ACTOR, finished, create, claim, report, decide

HEADERS = {"Authorization": "Bearer live-operator"}
OPERATOR = "/api/operator/contests/contest/submissions"
PARTICIPANT = "/api/contests/contest/submissions"


@pytest.fixture
def live(database, monkeypatch):
    monkeypatch.setattr(operator, "status_session", database)
    monkeypatch.setattr(participant, "status_session", database)
    monkeypatch.setattr(submission_status, "status_session", database)
    monkeypatch.setattr(store, "get_staff_by_access_token", lambda token: ACTOR if token == "live-operator" else None)
    return database


def wait(path=OPERATOR, ids=("submission",), **kwargs):
    params = [("submission_id", sid) for sid in ids] + [("wait_seconds", kwargs.pop("seconds", 0))]
    return client.get(path + ":wait", params=params, headers=kwargs.pop("headers", HEADERS), **kwargs)


def test_operator_batch_is_bounded_authorized_and_never_reads_source(live, leased):
    engine = live.kw["bind"]
    sql = []
    def observed(conn, cursor, statement, *args): sql.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        response = wait()
        assert response.status_code == 200, response.text
        item = response.json()["data"][0]
        assert item["submission_id"] == "submission" and item["source_code"] is None
        assert item["rejudging_progress"] is None
        assert item["rejudged"] is False
        assert all("length(" in query.lower() for query in sql if "submissions.source_code" in query)
        assert wait(headers={}).status_code == 401
        assert wait(ids=("submission", "missing")).status_code == 404
        assert wait(ids=tuple(str(i) for i in range(21))).status_code == 422
        assert wait(seconds=3).status_code == 422
        assert client.get(OPERATOR + ":wait", headers=HEADERS).status_code == 422
        assert wait(ids=("submission", "submission")).json()["data"] == [item]
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_operator_wait_returns_progress_without_refreshing_or_changing_verdict(live, finished, monkeypatch):
    batch = create()
    job = claim(finished)[0]
    original = store.get_submission("submission").model_dump(mode="json")
    checks = []
    async def changed(versions, seconds):
        # The wait starts with no SQL connection held, even across a heartbeat.
        assert live.kw["bind"].pool.checkedout() == 0
        checks.append(versions)
        store.update_judge_progress(job["judge_job_id"], SECRET, job["lease_token"],
            SubmissionStatus.JUDGING, 1, 4, progress_phase="tle_confirmation")
    monkeypatch.setattr(operator, "wait_for_submission_changes", changed)
    response = wait(seconds=2)
    assert response.status_code == 200, response.text
    item = response.json()["data"][0]
    assert item["status"] == "wrong_answer"
    assert item["rejudging_progress"] == {"rejudging_id": batch["rejudging_id"], "status": "judging",
        "progress_current": 1, "progress_total": 4, "progress_percent": 25,
        "progress_phase": "tle_confirmation", "batch_status": "pending"}
    assert checks and store.get_submission("submission").model_dump(mode="json") == original
    assert client.get(OPERATOR + "/submission", headers=HEADERS).json()["data"]["rejudging_progress"] == item["rejudging_progress"]
    assert client.get(OPERATOR, headers=HEADERS).json()["data"][0]["rejudging_progress"] == item["rejudging_progress"]
    report(job)
    ready = wait().json()["data"][0]
    assert ready["status"] == "wrong_answer" and ready["rejudging_progress"]["status"] == "accepted"
    assert ready["rejudging_progress"]["batch_status"] == "ready" and not ready["rejudged"]
    decide(batch, "apply")
    applied = wait().json()["data"][0]
    assert applied["status"] == "accepted" and applied["rejudging_progress"] is None
    assert applied["rejudged"] is True


def test_wait_reauthorizes_after_long_poll(live, leased, monkeypatch):
    async def revoke(versions, seconds):
        monkeypatch.setattr(store, "get_staff_by_access_token", lambda token: None)
    monkeypatch.setattr(operator, "wait_for_submission_changes", revoke)
    assert wait(seconds=2).status_code == 401


def participant_auth(monkeypatch, *, visible=True, team="team", preview=False):
    def authorize(request, contest_id):
        if request.headers.get("authorization") != "Bearer participant":
            raise AppError(401, "authentication_required", "Login required")
        return {"team": SimpleNamespace(participant_team_id=team, contest_id="contest", team_name="Preview team"),
            "member": SimpleNamespace(name="Participant", email="participant@example.com"),
            "division": SimpleNamespace(division_id="division"), "is_preview": preview}
    monkeypatch.setattr(participant, "_require_contest_participant", authorize)
    monkeypatch.setattr(store, "get_contest", lambda cid: SimpleNamespace(participant_progress_visible=visible))


@pytest.mark.parametrize("visible", [True, False])
def test_participant_batch_preserves_own_team_and_progress_privacy(live, leased, monkeypatch, visible):
    participant_auth(monkeypatch, visible=visible)
    with live() as db:
        row = db.get(SubmissionRow, "submission")
        row.participant_team_id = "team"
        row.progress_current, row.progress_total, row.progress_phase = 1, 4, "tle_confirmation"
        row.compile_message, row.judge_message = "private compile", "private input"
        db.commit()
    item = wait(PARTICIPANT, headers={"Authorization": "Bearer participant"}).json()["data"][0]
    assert item["source_code"] == "" and item["compile_message"] is None and item["judge_message"] is None
    assert item["progress_current"] is None and item["progress_total"] is None
    assert item["progress_percent"] == (25 if visible else None)
    assert item["progress_phase"] == ("tle_confirmation" if visible else None)
    assert "rejudging_progress" not in item
    assert wait(PARTICIPANT, headers={}).status_code == 401
    participant_auth(monkeypatch, team="other-team")
    assert wait(PARTICIPANT, headers={"Authorization": "Bearer participant"}).status_code == 404


def test_participant_batch_supports_only_the_current_preview_owner(live, leased, monkeypatch):
    participant_auth(monkeypatch, preview=True)
    with live() as db:
        row = db.get(SubmissionRow, "submission")
        row.submission_kind, row.submitted_by_email = "participant_preview", "participant@example.com"
        db.commit()
    assert wait(PARTICIPANT, headers={"Authorization": "Bearer participant"}).status_code == 200
    with live() as db:
        db.get(SubmissionRow, "submission").submitted_by_email = "other@example.com"
        db.commit()
    assert wait(PARTICIPANT, headers={"Authorization": "Bearer participant"}).status_code == 404


def test_submission_history_is_paginated_and_preserves_original_candidate(live, finished):
    first = create()
    report(claim(finished)[0])
    decide(first, "apply")
    second = create()
    decide(second, "cancel")
    third = create()
    job = claim(finished)[0]
    store.update_judge_progress(job["judge_job_id"], SECRET, job["lease_token"], SubmissionStatus.JUDGING, 0, 1)
    path = OPERATOR + "/submission/rejudgings"
    response = client.get(path + "?limit=2", headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["total"], data["offset"], data["limit"], data["has_next"]) == (3, 0, 2, True)
    assert [item["status"] for item in data["items"]] == ["pending", "cancelled"]
    item = data["items"][0]
    assert item["requested_by_name"] == "Operator" and item["reason"] == "Fix testcase"
    assert item["original_result"]["status"] == "accepted"
    assert item["candidate_result"]["status"] == "judging"
    assert item["rejudging_progress"]["progress_percent"] == 0
    older = client.get(path + "?offset=2&limit=2", headers=HEADERS).json()["data"]
    assert older["items"][0]["status"] == "applied" and not older["has_next"]
    assert older["items"][0]["original_result"]["status"] == "wrong_answer"
    assert older["items"][0]["candidate_result"]["status"] == "accepted"
    assert older["items"][0]["rejudging_progress"] is None
    assert client.get(path).status_code == 401
    assert client.get(path.replace("/submission/", "/missing/"), headers=HEADERS).status_code == 404


def test_monitor_progress_changes_wake_all_listeners_without_terminal_result(live, leased, monkeypatch):
    monkeypatch.setattr(submission_status, "CHECK_INTERVAL", 0.01)
    async def check():
        baseline = submission_status.submission_versions(["submission"])
        monitor = submission_status.SubmissionMonitor()
        tasks = [asyncio.create_task(monitor.wait_changes(baseline, 2)) for _ in range(30)]
        await asyncio.sleep(0.03)
        with live() as db:
            row = db.get(SubmissionRow, "submission")
            row.progress_current, row.progress_total, row.status_updated_at = 1, 4, now_utc()
            db.commit()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.5)
        assert not monitor.change_watchers and monitor.task is None
    asyncio.run(check())


def test_monitor_wakes_for_staged_progress_and_cleans_cancellation(live, finished, monkeypatch):
    batch = create()
    job = claim(finished)[0]
    monkeypatch.setattr(submission_status, "CHECK_INTERVAL", 0.01)
    async def check():
        baseline = submission_status.submission_versions(["submission"])
        monitor = submission_status.SubmissionMonitor()
        task = asyncio.create_task(monitor.wait_changes(baseline, 2))
        await asyncio.sleep(0.03)
        store.update_judge_progress(job["judge_job_id"], SECRET, job["lease_token"], SubmissionStatus.JUDGING, 1, 4)
        await asyncio.wait_for(task, timeout=0.5)
        assert not monitor.change_watchers and monitor.task is None
        task = asyncio.create_task(monitor.wait_changes(submission_status.submission_versions(["submission"]), 2))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not monitor.change_watchers and monitor.task is None
    asyncio.run(check())


def test_bulk_rejudging_batch_error_is_visible_on_other_finished_item_without_n_plus_one(live, finished):
    with live() as db:
        db.add(SubmissionRow(submission_id="second", contest_id="contest", division_id="division",
            problem_id="problem", language="python311", source_code="print(42)", status="wrong_answer"))
        db.commit()
    batch = create(problem_id="problem")
    jobs = claim(finished)
    assert len(jobs) == 2
    report(jobs[0])
    report(jobs[1], SubmissionStatus.SYSTEM_ERROR)
    sql = []
    engine = live.kw["bind"]
    def observed(conn, cursor, statement, *args): sql.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        progress = rejudging.active_progress(store, "contest", [jobs[0]["submission_id"]])
        single_queries = len(sql)
        sql.clear()
        both = rejudging.active_progress(store, "contest", ["submission", "second"])
        assert len(sql) == single_queries == 2
        assert len(both) == 2
        assert progress[jobs[0]["submission_id"]]["status"] == "accepted"
        assert progress[jobs[0]["submission_id"]]["batch_status"] == "blocked"
        assert not rejudging.applied_submissions(store, "contest", ["submission", "second"])
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_monitor_database_error_does_not_create_a_false_change_or_leak_waiters(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    value, calls = {"submission": ("old",)}, []
    def versions(ids):
        calls.append(list(ids))
        if len(calls) == 1: raise SQLAlchemyError("temporary DB error")
        return value.copy()
    monkeypatch.setattr(submission_status, "submission_versions", versions)
    monkeypatch.setattr(submission_status, "CHECK_INTERVAL", 0.01)
    async def check():
        monitor = submission_status.SubmissionMonitor()
        task = asyncio.create_task(monitor.wait_changes(value.copy(), 2))
        await asyncio.sleep(0.04)
        assert not task.done()
        value["submission"] = ("new",)
        await asyncio.wait_for(task, timeout=0.3)
        assert len(calls) >= 2 and not monitor.change_watchers and monitor.task is None
        await asyncio.wait_for(monitor.wait_changes(value.copy(), 0.03), timeout=0.3)
        assert not monitor.change_watchers and monitor.task is None
    asyncio.run(check())


def test_pending_participant_wait_rechecks_access_after_wakeup(live, leased, monkeypatch):
    participant_auth(monkeypatch)
    with live() as db:
        db.get(SubmissionRow, "submission").participant_team_id = "team"
        db.commit()
    async def revoke(versions, seconds):
        def denied(*args, **kwargs): raise AppError(403, "participant_blocked", "Access revoked")
        monkeypatch.setattr(participant, "_require_contest_participant", denied)
    monkeypatch.setattr(participant, "wait_for_submission_changes", revoke)
    assert wait(PARTICIPANT, seconds=2, headers={"Authorization": "Bearer participant"}).status_code == 403


def test_operator_history_and_live_wait_require_submission_permission(live, leased, monkeypatch):
    from app.models import StaffAccount
    account = StaffAccount(staff_account_id="observer", email="observer@example.com", display_name="Observer",
        contest_scopes={"contest": ["contest.view"]}, is_service_master=False)
    monkeypatch.setattr(store, "get_staff_by_access_token", lambda token: account)
    assert wait().status_code == 403
    assert client.get(OPERATOR + "/submission/rejudgings", headers=HEADERS).status_code == 403


def test_monitor_no_change_snapshot_is_lightweight_in_fixed_db_batches(live, leased):
    engine = live.kw["bind"]
    sql = []
    def observed(conn, cursor, statement, *args): sql.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        versions = submission_status.submission_versions([str(i) for i in range(1001)])
        assert not versions and len(sql) == 3
        assert all("source_code" not in query and "scoring_evidence" not in query for query in sql)
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_monitor_does_not_treat_a_new_listener_as_missing_during_an_inflight_read(monkeypatch):
    from threading import Event
    entered, release = Event(), Event()
    values, calls = {"first": ("same",), "second": ("same",)}, []
    def versions(ids):
        calls.append(list(ids))
        if len(calls) == 1:
            entered.set()
            assert release.wait(1)
        return {sid: values[sid] for sid in ids}
    monkeypatch.setattr(submission_status, "submission_versions", versions)
    monkeypatch.setattr(submission_status, "CHECK_INTERVAL", 0.08)
    async def check():
        monitor = submission_status.SubmissionMonitor()
        first = asyncio.create_task(monitor.wait_changes({"first": values["first"]}, 2))
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(monitor.wait_changes({"second": values["second"]}, 2))
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0.03)
        assert not first.done() and not second.done()
        values["second"] = ("changed",)
        await asyncio.wait_for(second, timeout=0.5)
        assert not first.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        assert not monitor.change_watchers and monitor.task is None
    asyncio.run(check())
