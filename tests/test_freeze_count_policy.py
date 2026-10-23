"""Frozen counts must be independent of release strategy and hidden verdicts."""
from datetime import timedelta
import importlib.util
from pathlib import Path

import pytest
from test_scoreboard_release_modes import (
    race, client, public, release, release_url, set_mode,
    assert_presentation_matches, visible_scores,
)
from test_scoreboard_release_undo import undo
from app.models import ContestStatus, now_utc
from app.orm_models import ContestRow, ProblemRow, SubmissionRow, ScoreboardReleaseRow
from app.services.store import store


def cell(board, name="Green", code="B"):
    row = next(r for r in board["rows"] if r["team_name"] == name)
    return next(s for s in row["problem_scores"] if s["problem_code"] == code)


def add_attempt(race, name, code, minute, status):
    did = race["divisions"][0]
    with store._session() as db:
        problem = db.query(ProblemRow).filter_by(contest_id=race["cid"], division_id=did, problem_code=code).one()
        row = SubmissionRow(contest_id=race["cid"], division_id=did, problem_id=problem.problem_id,
            participant_team_id=race["teams"][did][name], language="cpp17", source_code="HIDDEN SOURCE",
            submitted_at=race["start"] + timedelta(minutes=minute), status=status)
        db.add(row)
        db.commit()


@pytest.mark.parametrize("mode", ["manual", "immediate", "resolver"])
@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_count_policy_across_modes_and_lifecycle(race, mode, ended, enabled):
    set_mode(race, mode)
    store.update_contest_settings(race["cid"], freeze_submission_count_visible=enabled,
        status=ContestStatus.ENDED if ended else ContestStatus.RUNNING,
        end_at=now_utc() - timedelta(minutes=1) if ended else now_utc() + timedelta(hours=1))
    add_attempt(race, "Green", "B", 65, "wrong_answer")
    add_attempt(race, "Green", "A", 80, "wrong_answer")  # already solved before freeze
    board = public(race)
    hidden = not (mode == "immediate" and ended)
    score = cell(board)
    if hidden:
        assert (score["attempts"], score["solved"], score["best_status"]) == (0, False, None)
        assert score.get("pending_attempts", 0) == (2 if enabled else 0)
        assert next(row for row in board["rows"] if row["team_name"] == "Green")["submission_count"] == 1
    else:
        assert score["solved"] and score["attempts"] == 1
        assert not score.get("pending_attempts")
    assert not cell(board, code="A").get("pending_attempts")
    assert_presentation_matches(race, board)
    internal = store.scoreboard_rows(race["cid"], race["divisions"][0], public_view=False)
    assert cell(internal)["solved"]
    assert next(row for row in internal["rows"] if row["team_name"] == "Green")["submission_count"] == 4


@pytest.mark.parametrize("mode", ["manual", "resolver", "immediate"])
def test_disabled_counts_and_hidden_verdicts_do_not_change_any_public_payload(race, mode):
    set_mode(race, mode)
    store.update_contest_settings(race["cid"], freeze_submission_count_visible=False,
        submission_access_after_end="public")
    if mode == "immediate":
        current = store.scoreboard_release(race["cid"], race["divisions"][0])
        release(race, "undo", expected_revision=current["revision"])
    endpoint = f"/api/contests/{race['cid']}/submissions?division_id={race['divisions'][0]}"
    before = public(race)
    listed = client.get(endpoint).json()
    assert len(listed["data"]) == 3  # only the 3 pre-freeze submissions per division
    add_attempt(race, "Green", "B", 66, "compile_error")
    add_attempt(race, "Green", "B", 67, "wrong_answer")
    add_attempt(race, "Green", "B", 90, "accepted")
    assert public(race) == before
    after = client.get(endpoint).json()
    assert after["data"] == listed["data"]
    assert after.get("page") == listed.get("page")
    assert "pending_attempts" not in str(before)
    assert "HIDDEN SOURCE" not in str(after)
    assert_presentation_matches(race, before)
    if mode != "immediate":
        release(race, "start")
        started = public(race)
        if mode == "resolver":
            assert started["release"]["resolver"]["total_steps"] == 2
            assert started["release"]["resolver"]["pending_count"] == 2
        # Rejudging and adding attempts must not change the frozen release snapshot.
        add_attempt(race, "Green", "B", 95, "wrong_answer")
        assert public(race) == started
        assert client.get(endpoint).json()["data"] == listed["data"]


@pytest.mark.parametrize("mode", ["manual", "resolver"])
def test_setting_toggles_cached_frozen_and_released_boards(race, mode):
    set_mode(race, mode)
    before = public(race)
    assert cell(before)["pending_attempts"] == 1
    release(race, "start")
    store.update_contest_settings(race["cid"], freeze_submission_count_visible=False)
    hidden = public(race)
    assert "pending_attempts" not in str(hidden)
    store.update_contest_settings(race["cid"], freeze_submission_count_visible=True)
    shown = public(race)
    assert cell(shown)["pending_attempts"] == 1
    assert visible_scores(hidden) == visible_scores(shown)
    if mode == "resolver":
        release(race, "next", expected_step=0)
        assert cell(public(race))["solved"]
        assert not cell(public(race)).get("pending_attempts")
    else:
        release(race, "rank", rank=2)  # Green's final rank
        assert cell(public(race))["solved"]
        assert not cell(public(race)).get("pending_attempts")
        assert cell(public(race), "Blue")["pending_attempts"] == 1
    release(race, "all")
    assert "pending_attempts" not in str(public(race))
    undo(race)
    assert cell(public(race), "Blue")["pending_attempts"] == 1


def test_before_freeze_live_override_and_exact_manual_freeze(race):
    future = now_utc() + timedelta(hours=1)
    store.update_contest_settings(race["cid"], status=ContestStatus.RUNNING,
        end_at=future + timedelta(hours=1), freeze_at=future)
    assert not public(race)["frozen"]
    assert "pending_attempts" not in str(public(race))
    store.update_contest_settings(race["cid"], scoreboard_freeze_mode="frozen")
    with store._session() as db:
        contest = db.get(ContestRow, race["cid"])
        contest.scoreboard_frozen_at = race["start"] + timedelta(minutes=60)
        db.commit()
    board = public(race)
    assert board["frozen"] and cell(board)["pending_attempts"] == 1
    store.update_contest_settings(race["cid"], scoreboard_freeze_mode="live")
    assert not public(race)["frozen"]
    assert cell(public(race))["solved"]


def test_grouped_problem_order_reranks_and_undo_restores_entire_cell(race):
    set_mode(race, "resolver")
    # Both problems are hidden for Green. B was submitted first, A must reveal first.
    did = race["divisions"][0]
    with store._session() as db:
        db.get(SubmissionRow, race["submissions"][did][("Green", "A")]).submitted_at = race["start"] + timedelta(minutes=90)
        db.commit()
    add_attempt(race, "Green", "A", 65, "wrong_answer")
    add_attempt(race, "Green", "A", 66, "wrong_answer")
    add_attempt(race, "Green", "A", 91, "wrong_answer")
    started = release(race, "start")
    assert started["resolver"]["total_steps"] == 3
    initial = public(race)
    one = release(race, "next", expected_step=0, expected_revision=started["revision"])
    assert one["resolver"]["last_event"]["problem_code"] == "A"
    assert one["resolver"]["last_event"]["status"] == "accepted"
    assert one["resolver"]["step"] == 1 and one["resolver"]["pending_count"] == 2
    assert cell(public(race), code="A")["attempts"] == 2
    assert cell(public(race), code="A")["solved"]
    after_one = public(race)
    assert_presentation_matches(race, after_one)
    # Blue is above Green at this point, so Green is still next. After B it jumps.
    two = release(race, "next", expected_step=1)
    assert two["resolver"]["last_event"]["team_name"] == "Green"
    assert two["resolver"]["last_event"]["problem_code"] == "B"
    three = release(race, "next", expected_step=2)
    assert three["resolver"]["last_event"]["team_name"] == "Blue"
    undo(race)
    undo(race)
    assert public(race) == after_one
    undo(race)
    assert public(race) == initial
    replay = release(race, "next", expected_step=0)
    assert replay["resolver"]["step"] == 1
    assert public(race) == after_one
    duplicate = client.post(release_url(race), headers=race["headers"], json={"action":"next", "expected_step":0})
    assert duplicate.status_code == 409


@pytest.mark.parametrize("statuses,wrong", [(["wrong_answer", "runtime_error"], 2),
    (["compile_error", "system_error"], 0), (["compile_error", "wrong_answer", "time_limit_exceeded"], 2)])
def test_all_failures_are_one_step_and_keep_existing_penalty_rules(race, statuses, wrong):
    set_mode(race, "resolver")
    did = race["divisions"][0]
    with store._session() as db:
        db.delete(db.get(SubmissionRow, race["submissions"][did][("Green", "B")]))
        db.commit()
    for index, status in enumerate(statuses):
        add_attempt(race, "Green", "B", 65 + index, status)
    release(race, "start")
    before = public(race)
    result = release(race, "next")
    assert result["resolver"]["step"] == 1
    score = cell(public(race))
    assert not score["solved"] and score["attempts"] == wrong
    assert not score.get("pending_attempts")
    undo(race)
    assert public(race) == before


def test_public_submission_paging_tracks_atomic_cell_release_and_undo(race):
    set_mode(race, "resolver")
    store.update_contest_settings(race["cid"], submission_access_after_end="public", freeze_submission_count_visible=False)
    add_attempt(race, "Green", "B", 65, "wrong_answer")
    add_attempt(race, "Green", "B", 66, "compile_error")
    did = race["divisions"][0]
    endpoint = f"/api/contests/{race['cid']}/submissions?division_id={did}&limit=1"
    def count():
        response = client.get(endpoint)
        assert response.status_code == 200
        return response.json()["page"]["total_count"]
    assert count() == 3
    release(race, "start")
    release(race, "next")
    assert count() == 6
    undo(race)
    assert count() == 3
    release(race, "all")
    assert count() == 7


def test_new_setting_default_and_idempotent_migration():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / "migrations/versions/0057_freeze_submission_counts.py"
    spec = importlib.util.spec_from_file_location("count_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with sa.create_engine("sqlite://").begin() as connection:
        connection.execute(sa.text("CREATE TABLE contests (contest_id TEXT PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO contests VALUES ('existing')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        assert connection.scalar(sa.text("SELECT freeze_submission_count_visible FROM contests")) == 0
        migration.downgrade()
        assert connection.scalar(sa.text("SELECT contest_id FROM contests")) == "existing"


def test_legacy_progress_resumes_in_problem_units_without_replaying_scores(race):
    set_mode(race, "resolver")
    add_attempt(race, "Green", "B", 65, "wrong_answer")
    release(race, "start")
    release(race, "next")
    before = public(race)
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, race["divisions"][0])
        legacy = dict(saved.resolver_state)
        legacy.pop("version")
        legacy.update(step=2, total_steps=3)
        saved.resolver_state = legacy
        db.commit()
    assert public(race) == before
    final = release(race, "next", expected_step=1)
    assert final["mode"] == "all"
    assert final["resolver"]["step"] == final["resolver"]["total_steps"] == 2
    undo(race)
    assert public(race) == before


def test_two_concurrent_next_requests_publish_only_one_whole_problem(race):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.database import engine
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    set_mode(race, "resolver")
    add_attempt(race, "Green", "B", 65, "wrong_answer")
    add_attempt(race, "Green", "B", 66, "wrong_answer")
    started = release(race, "start")
    barrier = Barrier(2)
    def advance():
        barrier.wait(timeout=5)
        try:
            return store.update_scoreboard_release(race["cid"], race["divisions"][0], "next",
                expected_step=0, expected_revision=started["revision"])
        except ValueError:
            return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: advance(), range(2)))
    assert sum(result == "stale" for result in results) == 1
    current = store.scoreboard_release(race["cid"], race["divisions"][0])
    assert current["revision"] == started["revision"] + 1
    assert current["resolver"]["step"] == 1
    score = cell(public(race))
    assert score["solved"] and score["attempts"] == 2
    undo(race)
    assert not cell(public(race))["solved"]
    assert cell(public(race))["pending_attempts"] == 3


def test_legacy_manual_release_applies_counts_without_exposing_final_scores(race):
    release(race, "start")
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, race["divisions"][0])
        saved.resolver_state = None
        db.commit()
    board = public(race)
    assert visible_scores(board) == [("Red", 1, 1, 15), ("Blue", 2, 1, 20), ("Green", 3, 1, 40)]
    assert cell(board)["pending_attempts"] == 1
    assert board["release"]["ranks"] == []
    store.update_contest_settings(race["cid"], freeze_submission_count_visible=False)
    assert "pending_attempts" not in str(public(race))
    release(race, "rank", rank=2)
    assert cell(public(race))["solved"]


@pytest.mark.parametrize("enabled", [False, True])
def test_display_credentials_use_same_count_projection_as_participants(race, enabled):
    from app.services import presentation_access
    set_mode(race, "resolver")
    store.update_contest_settings(race["cid"], freeze_submission_count_visible=enabled)
    account = presentation_access.issue_account(race["cid"])
    token = presentation_access.login(account["email"], "isolated-count-test")["access_token"]
    headers = {"Authorization": "Bearer " + token}
    endpoint = f"/api/presentation/contests/{race['cid']}/scoreboard"
    for action in [None, "start", "next", "all"]:
        if action:
            release(race, action)
        expected = public(race)
        response = client.get(endpoint, headers=headers)
        assert response.status_code == 200
        section = next(section for section in response.json()["data"]["sections"] if section["division"]["division_id"] == race["divisions"][0])
        assert section["rows"] == expected["rows"]
        assert section["release"] == expected["release"]
