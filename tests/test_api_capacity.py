import asyncio
from datetime import timedelta
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update

from app.database import engine
from app.main import app
from app.models import now_utc
from app.orm_models import SubmissionRow, TeamSessionRow
from app.services import result_cache, submission_status
from app.services.request_capacity import RequestCapacityMiddleware
from app.services.store import store
from app.services.security import token_hash
from app.settings import settings
from test_performance_controls import board, cache  # shared synthetic board/Redis fixtures


def test_other_contest_and_progress_writes_do_not_flush_board(cache, board):
    cid, did, sid, _ = board
    other = str(uuid4())
    calls = []
    def read_other():
        return result_cache.cached_result(f"scoreboard:{other}", "same", 30,
            lambda: calls.append(1) or {"unchanged": True})
    read_other()
    before = store.scoreboard_rows(cid, did)
    with store._session() as db:
        row = db.get(SubmissionRow, sid)
        row.progress_current = 42
        db.commit()
        db.execute(update(SubmissionRow).where(SubmissionRow.submission_id == sid).values(runtime_ms=99))
        db.commit()
    queries = []
    def observed(conn, cursor, statement, *args):
        queries.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        assert store.scoreboard_rows(cid, did) == before
        assert not any("FROM submissions" in q for q in queries)
    finally:
        event.remove(engine, "before_cursor_execute", observed)
    with store._session() as db:
        db.execute(update(SubmissionRow).where(SubmissionRow.submission_id == sid).values(status="wrong_answer"))
        db.commit()
    assert store.scoreboard_rows(cid, did)["rows"][0]["solved"] == 0
    assert read_other() == {"unchanged": True} and len(calls) == 1


def test_serialized_cache_has_identical_dates_and_isolated_values(cache):
    moment = now_utc()
    value = {"date": moment, "rows": [1]}
    def read():
        return result_cache.cached_result("json-test", "same", 2, lambda: value, serialized=True)
    first = read()
    assert read() == first
    assert json.loads(first)["date"] == moment.isoformat()


def test_session_auth_reads_without_repeated_writes_and_revokes_immediately(board):
    cid, did, sid, _ = board
    with store._session() as db:
        submission = db.get(SubmissionRow, sid)
        token = uuid4().hex
        session = TeamSessionRow(contest_id=cid, division_id=did,
            participant_team_id=submission.participant_team_id, team_member_id=submission.team_member_id,
            access_token_hash=token_hash(token), expires_at=now_utc()+timedelta(hours=1),
            last_seen_at=now_utc())
        db.add(session)
        db.commit()
        session_id = session.team_session_id
    sql = []
    def observed(conn, cursor, statement, *args):
        sql.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        assert store.get_participant_by_access_token(cid, token)
        assert store.get_participant_by_access_token(cid, token)
        assert not any(q.lstrip().upper().startswith("UPDATE") for q in sql)
        # Identity and team members share one current authorization query.
        assert len([q for q in sql if q.lstrip().upper().startswith("SELECT")]) == 2
        with store._session() as db:
            db.get(TeamSessionRow, session_id).last_seen_at = now_utc()-timedelta(minutes=1)
            db.commit()
        sql.clear()
        assert store.get_participant_by_access_token(cid, token)
        assert sum(q.lstrip().upper().startswith("UPDATE") for q in sql) == 1
        with store._session() as db:
            db.get(TeamSessionRow, session_id).revoked_at = now_utc()
            db.commit()
        assert store.get_participant_by_access_token(cid, token) is None
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_thousand_status_waits_share_queries_and_release_all_watchers(monkeypatch):
    checks = []
    monkeypatch.setattr(submission_status, "pending_submissions", lambda ids: checks.append(ids) or set())
    async def verify():
        monitor = submission_status.SubmissionMonitor()
        await asyncio.gather(*(monitor.wait("same", 2) for _ in range(1000)))
        assert checks == [["same"]]
        assert monitor.watchers == {} and monitor.task is None
    asyncio.run(verify())


def test_status_batches_never_read_source_or_make_per_user_queries():
    sql = []
    def observed(conn, cursor, statement, *args):
        sql.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        assert submission_status.pending_submissions([str(uuid4()) for _ in range(1001)]) == set()
        assert len(sql) == 3
        assert all("source_code" not in query for query in sql)
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_admission_is_bounded_without_starving_health_or_long_polls(monkeypatch):
    monkeypatch.setattr(settings, "api_max_inflight_requests", 1)
    async def verify():
        entered, release = asyncio.Event(), asyncio.Event()
        async def endpoint(scope, receive, send):
            if scope["path"] == "/api/slow":
                entered.set()
                await release.wait()
            await send({"type":"http.response.start", "status":200, "headers":[]})
            await send({"type":"http.response.body", "body":b"ok"})
        middleware = RequestCapacityMiddleware(endpoint)
        async def call(path):
            sent = []
            async def send(value): sent.append(value)
            await middleware({"type":"http", "path":path}, None, send)
            return sent[0]["status"]
        slow = asyncio.create_task(call("/api/slow"))
        await entered.wait()
        assert await call("/api/another") == 503
        assert await call("/api/health") == 200
        assert await call("/api/internal/judge/test") == 200
        assert await call("/api/status:wait") == 200
        release.set()
        assert await slow == 200
        assert middleware.active == {"request":0,"wait":0,"stream":0}
        assert await call("/api/another") == 200
    asyncio.run(verify())


def test_one_token_cannot_take_all_capacity_and_cancellation_releases_it(monkeypatch):
    monkeypatch.setattr(settings, "api_max_account_requests", 1)
    async def verify():
        entered, release = asyncio.Event(), asyncio.Event()
        async def endpoint(scope, receive, send):
            if scope["path"] == "/api/slow":
                entered.set()
                await release.wait()
            await send({"type":"http.response.start","status":200,"headers":[]})
        middleware = RequestCapacityMiddleware(endpoint)
        async def call(token, path):
            sent = []
            async def send(value): sent.append(value)
            await middleware({"type":"http", "path":path,
                "headers":[(b"authorization",token)]}, None, send)
            return sent[0]["status"]
        slow = asyncio.create_task(call(b"Bearer a", "/api/slow"))
        await entered.wait()
        assert await call(b"Bearer a", "/api/fast") == 429
        assert await call(b"bearer a", "/api/fast") == 429
        assert await call(b"BEARER a", "/api/fast") == 429
        assert await call(b"Bearer b", "/api/fast") == 200
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)
        assert middleware.accounts == {} and middleware.active["request"] == 0
        assert await call(b"Bearer a", "/api/fast") == 200
    asyncio.run(verify())


def test_etag_never_bypasses_access_policy(board, cache):
    cid, did, _, _ = board
    store.update_contest_settings(cid, scoreboard_access_after_end="public", scoreboard_public_during_contest=True)
    client = TestClient(app)
    path = f"/api/contests/{cid}/divisions/{did}/scoreboard"
    first = client.get(path)
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert client.get(path, headers={"If-None-Match":etag}).status_code == 304
    assert client.get(path, headers={"If-None-Match":etag.removeprefix("W/")}).status_code == 304
    assert first.headers["x-request-id"]
    identity = client.get(path, headers={"Accept-Encoding":"gzip;q=0"})
    assert identity.status_code == 200 and "content-encoding" not in identity.headers
    assert identity.json()["data"] == first.json()["data"]
    store.update_contest_settings(cid, visibility="private")
    assert client.get(path, headers={"If-None-Match":etag}).status_code == 404


def test_problem_status_query_is_scoped_and_does_not_load_sources(board):
    cid, _, sid, _ = board
    with store._session() as db:
        row = db.get(SubmissionRow, sid)
        tid, pid = row.participant_team_id, row.problem_id
    sql = []
    def observed(conn, cursor, statement, *args): sql.append(statement)
    event.listen(engine, "before_cursor_execute", observed)
    try:
        assert store.participant_solve_statuses(cid, tid) == {pid:"accepted"}
        assert len(sql) == 1 and "source_code" not in sql[0]
        assert "contest_id" in sql[0] and "participant_team_id" in sql[0]
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_cross_process_cache_misses_compute_once():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("Requires a disposable Redis database")
    import redis
    client = redis.Redis.from_url(url)
    identity = uuid4().hex
    counter = "capacity-test:"+identity
    code = """
import time
from app.services import result_cache as cache
def compute():
    cache._client(cache.settings.redis_url).incr(%r)
    time.sleep(0.2)
    return {'rows':[1,2,3]}
print(cache.cached_result('scoreboard:capacity-test', %r, 5, compute, serialized=True))
""" % (counter, identity)
    env = {**os.environ, "REDIS_URL":url}
    processes = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env) for _ in range(6)]
    try:
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            assert json.loads(output) == {"rows":[1,2,3]}
        assert int(client.get(counter)) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        client.delete(counter)


def test_refresh_serves_recent_same_policy_snapshot_without_blocking(monkeypatch, cache):
    clock = [100.0]
    monkeypatch.setattr(result_cache.time, "time", lambda: clock[0])
    identity = uuid4().hex
    entered, release = Event(), Event()
    calls = []
    def compute():
        calls.append(1)
        if len(calls) > 1:
            entered.set()
            assert release.wait(timeout=3)
        return {"generation":len(calls)}
    def read():
        return result_cache.cached_result("scoreboard:swr", identity, 2, compute,
            serialized=True, snapshot=True)
    assert json.loads(read()) == {"generation":1}
    clock[0] = 102.0
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read)
        try:
            assert entered.wait(timeout=2)
            assert json.loads(read()) == {"generation":1}
            assert not future.done()
        finally:
            release.set()
        assert json.loads(future.result(timeout=2)) == {"generation":2}
    assert json.loads(read()) == {"generation":2}
    fresh_policy = result_cache.cached_result("scoreboard:swr", identity+"policy-change", 2,
        lambda:{"private":True}, serialized=True, snapshot=True)
    assert json.loads(fresh_policy) == {"private":True}


def test_other_contest_rebuild_uses_recent_snapshot_without_extending_it(monkeypatch, cache):
    from app.services.errors import AppError
    clock = [100.0]
    monkeypatch.setattr(result_cache.time, "time", lambda: clock[0])
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(result_cache, "_rebuild_slots", slots)
    identity = uuid4().hex
    calls = []
    def read(policy=identity):
        return result_cache.cached_result("scoreboard:budget", policy, 2,
            lambda:calls.append(1) or {"generation":len(calls)}, serialized=True, snapshot=True)
    assert json.loads(read()) == {"generation":1}
    clock[0] = 102.0
    slots.acquire()
    try:
        assert json.loads(read()) == {"generation":1}
        assert len(calls) == 1
        with pytest.raises(AppError) as caught:
            read(identity+"new-policy")
        assert caught.value.status_code == 503
    finally:
        slots.release()
    assert json.loads(read()) == {"generation":2}


def test_request_scope_reuses_connection_and_closes_it_before_next_request(board):
    from app.services.request_sessions import request_database
    cid, _, _, _ = board
    checkouts = []
    def observed(*args): checkouts.append(1)
    @request_database
    def read_twice():
        assert store.get_contest(cid)
        assert store.get_contest(cid)
    event.listen(engine, "checkout", observed)
    try:
        read_twice()
        assert len(checkouts) == 1
        read_twice()
        assert len(checkouts) == 2
    finally:
        event.remove(engine, "checkout", observed)


def test_postgres_statement_deadline_returns_retryable_busy_response():
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Requires disposable PostgreSQL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError
    from app.main import database_statement_error
    bounded = create_engine(settings.database_url, connect_args={"options":"-c statement_timeout=50"},
        pool_size=1, max_overflow=0)
    try:
        with pytest.raises(DBAPIError) as caught:
            with bounded.connect() as connection:
                connection.execute(text("SELECT pg_sleep(0.2)"))
        assert caught.value.orig.sqlstate == "57014"
        response = asyncio.run(database_statement_error(
            SimpleNamespace(state=SimpleNamespace(request_id="timeout-check")), caught.value))
        assert response.status_code == 503 and response.headers["retry-after"] == "1"
        assert json.loads(response.body)["error"]["code"] == "api_busy"
    finally:
        bounded.dispose()


def test_cancelled_status_caller_retains_capacity_until_sql_finishes(monkeypatch):
    from app.services.errors import AppError
    entered, release = Event(), Event()
    pool = ThreadPoolExecutor(max_workers=1)
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(submission_status, "_snapshot_workers", pool)
    monkeypatch.setattr(submission_status, "_snapshot_slots", slots)
    def slow():
        entered.set()
        assert release.wait(timeout=3)
        return "done"
    async def verify():
        running = asyncio.create_task(submission_status.read_status_snapshot(slow))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        with pytest.raises(AppError) as caught:
            await submission_status.read_status_snapshot(lambda:"unexpected")
        assert caught.value.status_code == 503
        release.set()
    try:
        asyncio.run(verify())
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert slots.acquire(blocking=False)
    slots.release()
