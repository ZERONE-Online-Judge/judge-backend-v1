"""ICPC tie rules agree in live standings and persisted resolver actions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.orm_models import ProblemRow, ScoreboardReleaseRow, SubmissionRow
from app.services import icpc_ranking
from app.services.store import store
from test_scoreboard_release_modes import race, client, public, release, release_url, set_mode


def row(name, solved=1, penalty=100, last="2026-11-15T01:20:00+00:00", wrong=0):
    return {"team_id": name, "team_name": name, "solved": solved,
            "penalty": penalty, "last_solved_at": last,
            "problem_scores": [{"solved": False, "attempts": wrong}]}


def standings(rows):
    return [(item["team_name"], item["rank"]) for item in rows]


@pytest.mark.parametrize("ranker", [icpc_ranking.rank_rows, store._rank_resolver_rows])
def test_solved_penalty_and_last_acceptance_are_the_only_rank_criteria(ranker):
    rows = [row("Earlier", wrong=50), row("Later", last="2026-11-15T01:40:00+00:00"),
            row("Less penalty", penalty=90, last="2026-11-15T02:00:00+00:00"),
            row("More solved", solved=2, penalty=500),
            row("Same instant", last="2026-11-15T10:20:00+09:00", wrong=2)]
    ranker(rows)
    assert standings(rows) == [("More solved", 1), ("Less penalty", 2),
                               ("Earlier", 3), ("Same instant", 3), ("Later", 5)]


@pytest.mark.parametrize("ranker", [icpc_ranking.rank_rows, store._rank_resolver_rows])
def test_zero_solve_teams_share_rank_even_with_wrong_attempts(ranker):
    rows = [row("Wrong only", solved=0, penalty=0, last=None, wrong=15),
            row("No submissions", solved=0, penalty=0, last=None),
            row("Compilation only", solved=0, penalty=0, last=None)]
    ranker(rows)
    assert standings(rows) == [("Compilation only", 1), ("No submissions", 1), ("Wrong only", 1)]


def test_sqlite_datetimes_and_json_timestamps_have_identical_rank_keys():
    naive = datetime(2026, 11, 15, 1, 20)
    assert icpc_ranking.rank_key(row("Naive", last=naive)) == icpc_ranking.rank_key(
        row("JSON", last="2026-11-15T10:20:00+09:00"))
    assert icpc_ranking.rank_key(row("UTC", last=naive.replace(tzinfo=timezone.utc))) == icpc_ranking.rank_key(
        row("JSON", last="2026-11-15T01:20:00Z"))


def set_submissions(race, entries):
    did = race["divisions"][0]
    with store._session() as db:
        problems = {item.problem_code: item.problem_id for item in db.query(ProblemRow).filter_by(division_id=did)}
        db.execute(delete(SubmissionRow).where(SubmissionRow.division_id == did))
        for team, code, minute, status in entries:
            db.add(SubmissionRow(contest_id=race["cid"], division_id=did,
                participant_team_id=race["teams"][did][team], problem_id=problems[code],
                language="cpp17", source_code="", status=status,
                submitted_at=race["start"] + timedelta(minutes=minute)))
        db.commit()


def tied_entries():
    return [("Red", "A", 100, "accepted"),
            ("Blue", "A", 65, "wrong_answer"), ("Blue", "A", 80, "accepted"),
            ("Blue", "B", 70, "wrong_answer"),
            ("Green", "A", 100, "accepted"), ("Green", "B", 75, "wrong_answer"),
            ("Green", "B", 85, "wrong_answer"),
            ("Blue", "A", 62, "compile_error"), ("Blue", "A", 63, "system_error"),
            ("Blue", "A", 110, "accepted")]


@pytest.mark.parametrize("strategy", ["manual", "manual_all", "resolver", "immediate"])
def test_live_and_each_release_strategy_use_the_same_official_ranks(race, strategy):
    set_submissions(race, tied_entries())
    set_mode(race, strategy)
    did = race["divisions"][0]
    internal = client.get(f"/api/operator/contests/{race['cid']}/divisions/{did}/scoreboard/internal",
                          headers=race["headers"])
    assert internal.status_code == 200, internal.text
    actual = internal.json()["data"]["rows"]
    assert standings(actual) == [("Blue", 1), ("Green", 2), ("Red", 2)]
    assert [item["penalty"] for item in actual] == [100, 100, 100]
    blue = actual[0]
    score = next(item for item in blue["problem_scores"] if item["problem_code"] == "A")
    assert score["wrong_attempts"] == 1
    assert score["attempts"] == 1
    assert blue["submission_count"] == 6  # CE, infrastructure error and post-AC submissions remain in history.
    if strategy != "immediate":
        frozen = public(race)
        assert all(item["rank"] == 1 and item["solved"] == 0 for item in frozen["rows"])
        blue_frozen = next(item for item in frozen["rows"] if item["team_name"] == "Blue")
        assert next(item for item in blue_frozen["problem_scores"] if item["problem_code"] == "A")["pending_attempts"] == 5
        release(race, "all")
    assert standings(public(race)["rows"]) == standings(actual)


def test_resolver_advances_and_undoes_with_official_ties(race):
    set_submissions(race, tied_entries())
    set_mode(race, "resolver")
    did = race["divisions"][0]
    release(race, "start")
    release(race, "next")  # Red A: currently the last frozen row.
    first = public(race)
    assert standings(first["rows"]) == [("Red", 1), ("Blue", 2), ("Green", 2)]
    second = release(race, "next")  # Green A: exact same last AC as Red.
    assert standings(public(race)["rows"]) == [("Green", 1), ("Red", 1), ("Blue", 3)]
    undone = client.post(release_url(race), headers=race["headers"], json={
        "action": "undo", "expected_revision": second["revision"]})
    assert undone.status_code == 200, undone.text
    assert public(race) == first
    while True:
        step = release(race, "next")
        if step["mode"] == "all":
            break
    assert standings(public(race)["rows"]) == [("Blue", 1), ("Green", 2), ("Red", 2)]
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, did)
        assert saved.resolver_state["ranking_policy"] == icpc_ranking.ICPC_RANKING_POLICY
        assert standings(saved.resolver_state["rows"]) == standings(saved.snapshot_rows)


def test_saved_resolver_keeps_its_original_ranking_policy_and_undo(race):
    set_submissions(race, tied_entries())
    set_mode(race, "resolver")
    did = race["divisions"][0]
    release(race, "start")
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, did)
        state = deepcopy(saved.resolver_state)
        state.pop("ranking_policy")  # An announcement created before this fix.
        snapshot = deepcopy(saved.snapshot_rows)
        store._rank_resolver_rows(snapshot, ranking_policy=None)
        saved.resolver_state, saved.snapshot_rows = state, snapshot
        db.commit()
    release(race, "next")
    first = public(race)
    second = release(race, "next")
    undone = client.post(release_url(race), headers=race["headers"], json={
        "action": "undo", "expected_revision": second["revision"]})
    assert undone.status_code == 200
    assert public(race) == first
    while release(race, "next")["mode"] != "all":
        pass
    assert standings(public(race)["rows"]) == standings(snapshot)
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, did)
        assert standings(saved.resolver_state["rows"]) == standings(snapshot)
