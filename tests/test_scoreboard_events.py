import asyncio
import time

import pytest
from sqlalchemy import event

from app.database import engine, SessionLocal
from app.orm_models import ContestRow
from app.routers import presentation
from app.services import scoreboard_events as events
from app.services import presentation_access
from app.services.errors import AppError
from app.services.store import store
from test_presentation_access import setup, client, login


class Connected:
    async def is_disconnected(self):
        return False


def test_stream_routes_keep_operator_and_display_credentials_separate(
    setup, monkeypatch
):
    cid, _, _, staff, account = setup
    display_token = login(account["email"]).json()["data"]["access_token"]
    display = {"Authorization": "Bearer " + display_token}
    operator_path = f"/api/operator/contests/{cid}/scoreboard/presentation/events"
    display_path = f"/api/presentation/contests/{cid}/scoreboard/events"
    for path in (operator_path, display_path):
        assert client.get(path).status_code == 401
    assert client.get(operator_path, headers=display).status_code == 401
    assert client.get(display_path, headers=staff).status_code == 401
    assert (
        client.get(
            "/api/presentation/contests/other/scoreboard/events", headers=display
        ).status_code
        == 401
    )

    async def finite(request, contest_id, authorize):
        authorize()
        yield "event: scoreboard_update\ndata: {}\n\n"

    monkeypatch.setattr(presentation, "scoreboard_events", finite)
    for path, headers in ((operator_path, staff), (display_path, display)):
        result = client.get(path, headers=headers)
        assert result.status_code == 200
        assert result.headers["content-type"].startswith("text/event-stream")
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-accel-buffering"] == "no"
        assert result.text == "event: scoreboard_update\ndata: {}\n\n"


@pytest.mark.parametrize("initial_counts", [False, True])
def test_versions_load_only_small_columns_and_follow_release_undo_and_policy(setup, initial_counts):
    cid, did, _, _, _ = setup
    with SessionLocal.begin() as db:
        contest = db.get(ContestRow, cid)
        contest.status = "ended"
        contest.freeze_submission_count_visible = initial_counts
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        before = events.reveal_versions({cid})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 2
    assert all(
        "snapshot_rows" not in sql and "resolver_state" not in sql for sql in statements
    )
    assert len(before[cid]) == 64
    started = store.update_scoreboard_release(cid, did, "start")
    after_start = events.reveal_versions({cid})
    assert before != after_start
    store.update_scoreboard_release(
        cid, did, "undo", expected_revision=started["revision"]
    )
    after_undo = events.reveal_versions({cid})
    assert after_start != after_undo
    with SessionLocal.begin() as db:
        db.get(ContestRow, cid).scoreboard_freeze_mode = "live"
    after_freeze = events.reveal_versions({cid})
    assert after_freeze != after_undo
    store.update_contest_settings(cid, freeze_submission_count_visible=initial_counts)
    assert events.reveal_versions({cid}) == after_freeze
    store.update_contest_settings(cid, freeze_submission_count_visible=not initial_counts)
    assert events.reveal_versions({cid}) != after_freeze
    assert events.reveal_versions({"missing"}) == {}


def test_selected_divisions_are_filtered_before_scoreboards_are_computed(
    setup, monkeypatch
):
    cid, did, _, staff, account = setup
    other = store.create_contest_division(cid, "B", "Other division").division_id
    outside = store.create_contest('Unrelated presentation', 'Test', '')
    outside_division = store.create_contest_division(outside.contest_id, 'C', 'Unrelated division').division_id
    available = [
        {'division_id': did, 'code': 'A', 'name': 'Division'},
        {'division_id': other, 'code': 'B', 'name': 'Other division'},
    ]
    display = {
        "Authorization": "Bearer "
        + login(account["email"]).json()["data"]["access_token"]
    }
    original = store.scoreboard_rows
    calls = []

    def board(contest_id, division_id, **kwargs):
        calls.append(division_id)
        return original(contest_id, division_id, **kwargs)

    monkeypatch.setattr(store, "scoreboard_rows", board)
    for endpoint, headers in (
        (f"/api/operator/contests/{cid}/scoreboard/presentation", staff),
        (f"/api/presentation/contests/{cid}/scoreboard", display),
    ):
        calls.clear()
        response = client.get(endpoint, headers=headers, params=[("division_id", did)])
        assert response.status_code == 200
        assert response.json()['data']['available_divisions'] == available
        assert outside_division not in response.text
        assert [
            part["division"]["division_id"]
            for part in response.json()["data"]["sections"]
        ] == [did]
        assert calls == [did]
        calls.clear()
        response = client.get(
            endpoint, headers=headers, params=[("division_id", "missing")]
        )
        assert response.status_code == 200 and response.json()["data"]["sections"] == []
        assert response.json()['data']['available_divisions'] == available
        assert calls == []
        response = client.get(
            endpoint,
            headers=headers,
            params=[("division_id", did), ("division_id", other)],
        )
        assert {
            part["division"]["division_id"]
            for part in response.json()["data"]["sections"]
        } == {did, other}
        assert response.json()['data']['available_divisions'] == available


def test_display_streams_survive_other_logins_and_both_reauthenticate_on_revoke(setup, monkeypatch):
    cid, _, _, _, account = setup
    first = login(account['email']).json()['data']['access_token']
    monkeypatch.setattr(events, 'AUTH_INTERVAL', 0.01)
    monkeypatch.setattr(events, 'reveal_versions', lambda ids: {key: 'v1' for key in ids})

    def authorize(token):
        return lambda: presentation_access.require_presentation(token, cid)

    async def run():
        initial = events.scoreboard_events(Connected(), cid, authorize(first))
        additional = None
        try:
            assert 'scoreboard_update' in await anext(initial)
            # Log in another screen while the first screen's stream is open.
            second = login(account['email']).json()['data']['access_token']
            assert first != second
            additional = events.scoreboard_events(Connected(), cid, authorize(second))
            assert 'scoreboard_update' in await anext(additional)
            for frame in await asyncio.wait_for(asyncio.gather(anext(initial), anext(additional)), 2):
                assert 'scoreboard_update' in frame and 'reauthenticate' not in frame
            presentation_access.revoke_account(cid)
            for frame in await asyncio.wait_for(asyncio.gather(anext(initial), anext(additional)), 2):
                assert frame == 'event: reauthenticate\ndata: {}\n\n'
            for stream in (initial, additional):
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
        finally:
            await initial.aclose()
            if additional is not None:
                await additional.aclose()
            assert not events.scoreboard_monitor().listeners

    asyncio.run(run())


@pytest.mark.parametrize("strategy,actions", [
    ("manual", ("start", "rank", "undo")),
    ("manual_all", ("all", "undo", "all")),
])
def test_committed_release_changes_reach_the_stream_without_score_payloads(setup, strategy, actions):
    cid, did, _, _, _ = setup
    with SessionLocal.begin() as db:
        db.get(ContestRow, cid).status = "ended"
        db.get(ContestRow, cid).scoreboard_release_mode = strategy

    async def run():
        stream = events.scoreboard_events(Connected(), cid, lambda: None)
        try:
            assert await anext(stream) == "event: scoreboard_update\ndata: {}\n\n"
            # Consume the monitor's initial version before changing another connection.
            await asyncio.wait_for(anext(stream), 2)
            for action in actions:
                release = store.scoreboard_release(cid, did)
                at = time.monotonic()
                store.update_scoreboard_release(
                    cid,
                    did,
                    action,
                    rank=1 if action == "rank" else None,
                    expected_revision=release["revision"],
                )
                frame = await asyncio.wait_for(anext(stream), 2)
                assert frame == "event: scoreboard_update\ndata: {}\n\n"
                assert time.monotonic() - at < 2
        finally:
            monitor = events.scoreboard_monitor()
            await stream.aclose()
            assert monitor.task is None and not monitor.listeners

    asyncio.run(run())


def test_monitor_batches_displays_and_cleans_up_all_listeners(monkeypatch):
    calls = []

    def versions(ids):
        calls.append(set(ids))
        return {cid: "initial" for cid in ids}

    monkeypatch.setattr(events, "reveal_versions", versions)

    async def run():
        monitor = events.ScoreboardMonitor()
        first = monitor.subscribe("a")
        second = monitor.subscribe("a")
        other = monitor.subscribe("b")
        await asyncio.wait_for(
            asyncio.gather(first.wait(), second.wait(), other.wait()), 2
        )
        assert calls == [{"a", "b"}]
        await monitor.unsubscribe("a", first)
        assert len(monitor.listeners["a"]) == 1
        await monitor.unsubscribe("a", second)
        assert "a" not in monitor.versions
        await monitor.unsubscribe("b", other)
        assert monitor.task is None and not monitor.listeners

    asyncio.run(run())


def test_stream_reauthenticates_before_delivering_more_events(monkeypatch):
    monkeypatch.setattr(
        events, "reveal_versions", lambda ids: {cid: "v" for cid in ids}
    )

    def reject():
        raise AppError(401, "authentication_required", "Expired")

    async def run():
        stream = events.scoreboard_events(Connected(), "a", reject)
        assert "scoreboard_update" in await anext(stream)
        assert (
            await asyncio.wait_for(anext(stream), 2)
            == "event: reauthenticate\ndata: {}\n\n"
        )
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not events.scoreboard_monitor().listeners

    asyncio.run(run())


def test_unavailable_database_does_not_report_a_reveal_or_logout(monkeypatch):
    from sqlalchemy.exc import OperationalError

    def unavailable(ids):
        raise OperationalError("unavailable", {}, Exception())

    monkeypatch.setattr(events, "reveal_versions", unavailable)
    monkeypatch.setattr(events, "CHECK_INTERVAL", 0.01)

    async def run():
        monitor = events.ScoreboardMonitor()
        changed = monitor.subscribe("a")
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(changed.wait(), 0.05)
        finally:
            await monitor.unsubscribe("a", changed)
        assert not monitor.listeners and monitor.task is None

    asyncio.run(run())
