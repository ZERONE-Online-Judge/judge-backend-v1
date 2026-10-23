"""Public visibility and persistence regressions for end-of-contest release modes."""
import os
from datetime import timedelta
from uuid import uuid4

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import ParticipantTeamRow, ProblemRow, ScoreboardReleaseRow, SubmissionRow
from app.services.store import store


client = TestClient(app)


def login(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={
        "email": email, "otp_code": "", "force_new_session": True,
    })
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["operator_session"]["access_token"]}


@pytest.fixture
def race():
    now = now_utc()
    start = now - timedelta(hours=3)
    contest = store.create_contest("Scoreboard release modes", "Test", "", start_at=start,
        freeze_at=start + timedelta(hours=1), end_at=start + timedelta(hours=2), status=ContestStatus.ENDED)
    store.update_contest_settings(contest.contest_id, scoreboard_access_after_end="public", freeze_submission_count_visible=True)
    divisions = [store.create_contest_division(contest.contest_id, code, code) for code in ("A", "B")]
    teams, submissions = {}, {}
    for division in divisions:
        did = division.division_id
        teams[did], submissions[did] = {}, {}
        with store._session() as db:
            problems = {}
            for index, code in enumerate(("A", "B"), start=1):
                problem = ProblemRow(contest_id=contest.contest_id, division_id=did, problem_code=code,
                    title="Problem " + code, statement="", time_limit_ms=1000, memory_limit_mb=128,
                    display_order=index)
                db.add(problem)
                db.flush()
                problems[code] = problem.problem_id
            for name, first_minute, hidden_minute in (("Red", 15, None), ("Blue", 20, 80), ("Green", 40, 70)):
                team_name = name if division == divisions[0] else f"{division.code}-{name}"
                team = ParticipantTeamRow(contest_id=contest.contest_id, division_id=did, team_name=team_name)
                db.add(team)
                db.flush()
                teams[did][name] = team.participant_team_id
                for code, minute in (("A", first_minute), ("B", hidden_minute)):
                    if minute is None:
                        continue
                    submission = SubmissionRow(contest_id=contest.contest_id, division_id=did,
                        problem_id=problems[code], participant_team_id=team.participant_team_id,
                        language="cpp17", source_code="SECRET SOURCE", status="accepted",
                        submitted_at=start + timedelta(minutes=minute))
                    db.add(submission)
                    db.flush()
                    submissions[did][(name, code)] = submission.submission_id
            db.commit()
    return {"cid": contest.contest_id, "divisions": [d.division_id for d in divisions],
        "teams": teams, "submissions": submissions, "start": start,
        "headers": login("test3@zoj.com")}


def release_url(race, division=None):
    return f"/api/operator/contests/{race['cid']}/divisions/{division or race['divisions'][0]}/scoreboard/release"


def settings_url(race):
    return f"/api/operator/contests/{race['cid']}/settings"


def set_mode(race, mode):
    response = client.patch(settings_url(race), headers=race["headers"], json={"scoreboard_release_mode": mode})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["scoreboard_release_mode"] == mode


def public(race, division=None):
    did = division or race["divisions"][0]
    response = client.get(f"/api/contests/{race['cid']}/divisions/{did}/scoreboard")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def release(race, action, **extra):
    response = client.post(release_url(race), headers=race["headers"], json={"action": action, **extra})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def assert_presentation_matches(race, board):
    response = client.get(f"/api/operator/contests/{race['cid']}/scoreboard/presentation", headers=race["headers"])
    assert response.status_code == 200, response.text
    section = next(item for item in response.json()["data"]["sections"]
        if item["division"]["division_id"] == race["divisions"][0])
    assert section["rows"] == board["rows"]
    assert section.get("release") == board.get("release")
    assert section["frozen"] == board["frozen"]


def visible_scores(board):
    return [(row["team_name"], row["rank"], row["solved"], row["penalty"]) for row in board["rows"]]


def test_manual_remains_default_and_keeps_frozen_scores_after_end(race):
    assert store.contests[race["cid"]].scoreboard_release_mode == "manual"
    board = public(race)
    assert board["frozen"] is True
    assert visible_scores(board) == [("Red", 1, 1, 15), ("Blue", 2, 1, 20), ("Green", 3, 1, 40)]
    assert_presentation_matches(race, board)


def test_immediate_waits_for_end_then_tracks_finishing_judges_without_snapshot(race):
    set_mode(race, "immediate")
    did = race["divisions"][0]
    hidden = race["submissions"][did][("Blue", "B")]
    store.update_contest_settings(race["cid"], status=ContestStatus.RUNNING, end_at=now_utc() + timedelta(hours=1))
    before = public(race)
    assert before["frozen"] is True
    assert [row["solved"] for row in before["rows"]] == [1, 1, 1]
    with store._session() as db:
        db.get(SubmissionRow, hidden).status = "judging"
        db.commit()
    store.update_contest_settings(race["cid"], status=ContestStatus.ENDED, end_at=now_utc() - timedelta(minutes=1))
    ended = public(race)
    assert ended["frozen"] is False
    assert [row["team_name"] for row in ended["rows"]] == ["Green", "Red", "Blue"]
    assert ended["release"]["strategy"] == "immediate"
    assert ended["release"]["mode"] == "all"
    with store._session() as db:
        assert db.get(ScoreboardReleaseRow, did) is None
        db.get(SubmissionRow, hidden).status = "accepted"
        db.commit()
    finished = public(race)
    assert visible_scores(finished) == [("Blue", 1, 2, 100), ("Green", 2, 2, 110), ("Red", 3, 1, 15)]
    assert_presentation_matches(race, finished)
    with store._session() as db:
        assert db.get(ScoreboardReleaseRow, did) is None


@pytest.mark.parametrize("action", ["start", "rank", "next", "all"])
def test_immediate_disallows_manual_release_actions(race, action):
    set_mode(race, "immediate")
    response = client.post(release_url(race), headers=race["headers"], json={"action": action, "rank": 1})
    assert response.status_code == 409, response.text


def test_resolver_starts_at_frozen_order_and_only_reveals_next_event(race):
    set_mode(race, "resolver")
    did, other = race["divisions"]
    before = public(race)
    other_before = public(race, other)
    assert before["release"]["mode"] == "not_started"
    pending_counts = {(row["team_name"], score["problem_code"]): score["pending_attempts"]
        for row in before["rows"] for score in row["problem_scores"]}
    assert pending_counts == {
        ("Red", "A"): 0, ("Red", "B"): 0,
        ("Blue", "A"): 0, ("Blue", "B"): 1,
        ("Green", "A"): 0, ("Green", "B"): 1,
    }
    assert_presentation_matches(race, before)
    # Counts before the resolver starts expose submissions, never their verdicts.
    with store._session() as db:
        db.get(SubmissionRow, race["submissions"][did][("Blue", "B")]).status = "wrong_answer"
        db.commit()
    assert public(race) == before
    with store._session() as db:
        db.get(SubmissionRow, race["submissions"][did][("Blue", "B")]).status = "accepted"
        db.commit()
    started = release(race, "start")
    assert started["strategy"] == "resolver"
    assert started["mode"] == "partial"
    assert started["resolver"]["step"] == 0
    assert started["resolver"]["total_steps"] == 2
    board = public(race)
    assert visible_scores(board) == visible_scores(before)
    # Final standings and unrevealed AC results must not travel with the public response.
    assert board["release"]["ranks"] == []
    assert board["release"]["resolver"]["last_event"] is None
    assert "SECRET SOURCE" not in str(board)
    for row in board["rows"]:
        for score in row["problem_scores"]:
            assert score["best_submission_id"] is None
            if score["problem_code"] == "B":
                assert not score["solved"]
                assert score["best_status"] is None
    assert_presentation_matches(race, board)
    step_one = release(race, "next")
    assert step_one["resolver"]["step"] == 1
    event = step_one["resolver"]["last_event"]
    assert event["team_id"] == race["teams"][did]["Green"]
    assert (event["problem_code"], event["status"], event["from_rank"], event["to_rank"]) == ("B", "accepted", 3, 1)
    board = public(race)
    assert visible_scores(board) == [("Green", 1, 2, 110), ("Red", 2, 1, 15), ("Blue", 3, 1, 20)]
    assert board["release"]["ranks"] == []
    assert_presentation_matches(race, board)
    step_two = release(race, "next")
    assert step_two["resolver"]["step"] == 2
    assert step_two["mode"] == "all"
    board = public(race)
    assert visible_scores(board) == [("Blue", 1, 2, 100), ("Green", 2, 2, 110), ("Red", 3, 1, 15)]
    assert board["frozen"] is False
    assert_presentation_matches(race, board)
    assert public(race, other) == other_before


def test_resolver_resume_and_all_keep_the_original_snapshot_after_rejudge(race):
    set_mode(race, "resolver")
    did = race["divisions"][0]
    release(race, "start")
    release(race, "next")
    board_before = public(race)
    resumed = release(race, "start")
    assert resumed["resolver"]["step"] == 1
    assert public(race) == board_before
    with store._session() as db:
        db.get(SubmissionRow, race["submissions"][did][("Blue", "B")]).status = "wrong_answer"
        db.commit()
    assert public(race) == board_before
    all_result = release(race, "all")
    assert all_result["mode"] == "all"
    board = public(race)
    assert visible_scores(board) == [("Blue", 1, 2, 100), ("Green", 2, 2, 110), ("Red", 3, 1, 15)]
    assert release(race, "all") == all_result
    assert release(race, "start") == all_result
    assert public(race) == board


@pytest.mark.parametrize("mode,action", [("manual", "next"), ("resolver", "rank")])
def test_release_strategy_rejects_incompatible_actions(race, mode, action):
    set_mode(race, mode)
    release(race, "start")
    response = client.post(release_url(race), headers=race["headers"], json={"action": action, "rank": 1})
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("action", ["start", "all"])
def test_resolver_waits_until_ended_and_all_participant_jobs_finish(race, action):
    set_mode(race, "resolver")
    store.update_contest_settings(race["cid"], status=ContestStatus.RUNNING, end_at=now_utc() + timedelta(hours=1))
    response = client.post(release_url(race), headers=race["headers"], json={"action": action})
    assert response.status_code == 409
    store.update_contest_settings(race["cid"], status=ContestStatus.ENDED, end_at=now_utc() - timedelta(minutes=1))
    did = race["divisions"][0]
    with store._session() as db:
        db.get(SubmissionRow, race["submissions"][did][("Blue", "B")]).status = "judging"
        db.commit()
    response = client.post(release_url(race), headers=race["headers"], json={"action": action})
    assert response.status_code == 409
    with store._session() as db:
        assert db.get(ScoreboardReleaseRow, did) is None


@pytest.mark.parametrize("initial", ["manual", "resolver"])
def test_release_mode_is_locked_after_any_division_starts(race, initial):
    set_mode(race, initial)
    response = client.post(release_url(race, race["divisions"][1]), headers=race["headers"], json={"action": "start"})
    assert response.status_code == 200
    for next_mode in {"manual", "immediate", "resolver"} - {initial}:
        response = client.patch(settings_url(race), headers=race["headers"], json={"scoreboard_release_mode": next_mode})
        assert response.status_code == 409, response.text
    unchanged = client.patch(settings_url(race), headers=race["headers"], json={"scoreboard_release_mode": initial})
    assert unchanged.status_code == 200
    assert unchanged.json()["data"]["scoreboard_release_locked"] is True


def test_release_modes_preserve_after_end_access_controls(race):
    set_mode(race, "immediate")
    store.update_contest_settings(race["cid"], scoreboard_access_after_end="private")
    did = race["divisions"][0]
    assert client.get(f"/api/contests/{race['cid']}/divisions/{did}/scoreboard").status_code == 404
    response = client.get(f"/api/operator/contests/{race['cid']}/scoreboard/presentation", headers=race["headers"])
    assert response.status_code == 200
    section = next(item for item in response.json()["data"]["sections"] if item["division"]["division_id"] == did)
    assert section["frozen"] is False
    assert [row["solved"] for row in section["rows"]] == [2, 2, 1]


def test_settings_and_scoreboard_management_permissions_remain_separate(race):
    store.upsert_contest_operator(race["cid"], f"owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    tokens = {}
    for role in ("scoreboard_viewer", "scoreboard_manager", "settings_manager"):
        account = store.upsert_contest_operator(race["cid"], f"release-{role}-{uuid4().hex}@zoj.com", role, [role])
        tokens[role] = login(str(account.email))
    for role in ("scoreboard_viewer", "scoreboard_manager"):
        response = client.patch(settings_url(race), headers=tokens[role], json={"scoreboard_release_mode": "resolver"})
        assert response.status_code == 403, response.text
    for role in ("scoreboard_viewer", "settings_manager"):
        assert client.post(release_url(race), headers=tokens[role], json={"action": "next"}).status_code == 403
    assert client.patch(settings_url(race), headers=tokens["settings_manager"], json={"scoreboard_release_mode": "resolver"}).status_code == 200
    assert client.get(release_url(race), headers=tokens["scoreboard_viewer"]).status_code == 200
    assert client.post(release_url(race), headers=tokens["scoreboard_manager"], json={"action": "start"}).status_code == 200
    assert client.post(release_url(race), headers=tokens["scoreboard_manager"], json={"action": "next"}).status_code == 200
    assert client.post(release_url(race), json={"action": "next"}).status_code == 401


def test_resolver_next_checks_expected_step_to_prevent_double_advancement(race):
    set_mode(race, "resolver")
    response = client.post(release_url(race), headers=race["headers"], json={"action": "next", "expected_step": 0})
    assert response.status_code == 409
    release(race, "start")
    first = release(race, "next", expected_step=0)
    assert first["resolver"]["step"] == 1
    duplicate = client.post(release_url(race), headers=race["headers"], json={"action": "next", "expected_step": 0})
    assert duplicate.status_code == 409
    assert client.get(release_url(race), headers=race["headers"]).json()["data"]["resolver"]["step"] == 1
    last = release(race, "next", expected_step=1)
    assert last["mode"] == "all"
    assert release(race, "next", expected_step=2) == last


def test_resolver_compile_errors_are_revealed_without_penalty(race):
    set_mode(race, "resolver")
    did = race["divisions"][0]
    with store._session() as db:
        accepted = db.get(SubmissionRow, race["submissions"][did][("Green", "B")])
        db.add(SubmissionRow(contest_id=race["cid"], division_id=did, problem_id=accepted.problem_id,
            participant_team_id=accepted.participant_team_id, language="cpp17", source_code="private compilation failure",
            status="compile_error", submitted_at=race["start"] + timedelta(minutes=65)))
        db.commit()
    green_before = next(row for row in public(race)["rows"] if row["team_name"] == "Green")
    assert next(score for score in green_before["problem_scores"] if score["problem_code"] == "B")["pending_attempts"] == 2
    started = release(race, "start")
    assert started["resolver"]["total_steps"] == 2
    first = release(race, "next")
    assert first["resolver"]["last_event"]["status"] == "accepted"
    assert first["resolver"]["step"] == 1
    assert visible_scores(public(race))[0] == ("Green", 1, 2, 110)


def test_resolver_accumulates_failures_before_and_after_freeze(race):
    set_mode(race, "resolver")
    did = race["divisions"][0]
    with store._session() as db:
        accepted = db.get(SubmissionRow, race["submissions"][did][("Green", "B")])
        for minute in (50, 65):
            db.add(SubmissionRow(contest_id=race["cid"], division_id=did, problem_id=accepted.problem_id,
                participant_team_id=accepted.participant_team_id, language="cpp17", source_code="wrong",
                status="wrong_answer", submitted_at=race["start"] + timedelta(minutes=minute)))
        db.commit()
    release(race, "start")
    failed = release(race, "next")
    assert failed["resolver"]["last_event"]["status"] == "accepted"
    green = next(row for row in public(race)["rows"] if row["team_name"] == "Green")
    score = next(score for score in green["problem_scores"] if score["problem_code"] == "B")
    assert score["attempts"] == 2
    assert score["solved"]
    green = next(row for row in public(race)["rows"] if row["team_name"] == "Green")
    assert (green["solved"], green["penalty"]) == (2, 150)
    release(race, "next")
    assert visible_scores(public(race)) == [("Blue", 1, 2, 100), ("Green", 2, 2, 150), ("Red", 3, 1, 15)]


def test_resolver_later_submissions_after_an_ac_never_change_score(race):
    set_mode(race, "resolver")
    did = race["divisions"][0]
    with store._session() as db:
        accepted = db.get(SubmissionRow, race["submissions"][did][("Green", "B")])
        db.add(SubmissionRow(contest_id=race["cid"], division_id=did, problem_id=accepted.problem_id,
            participant_team_id=accepted.participant_team_id, language="cpp17", source_code="wrong after accepted",
            status="wrong_answer", submitted_at=race["start"] + timedelta(minutes=75)))
        db.commit()
    started = release(race, "start")
    # Pending counts include every hidden attempt, so they do not expose where the AC occurs.
    assert started["resolver"]["total_steps"] == 2
    release(race, "next")
    release(race, "next")
    before = visible_scores(public(race))
    assert before == [("Blue", 1, 2, 100), ("Green", 2, 2, 110), ("Red", 3, 1, 15)]
    last = release(race, "next")
    assert last["resolver"]["last_event"]["status"] == "accepted"
    assert last["mode"] == "all"
    assert visible_scores(public(race)) == before


def test_resolver_can_publish_everything_without_starting_animation(race):
    set_mode(race, "resolver")
    completed = release(race, "all")
    assert completed["mode"] == "all"
    assert completed["resolver"]["step"] == completed["resolver"]["total_steps"] == 2
    assert completed["resolver"]["pending_count"] == 0
    assert visible_scores(public(race)) == [("Blue", 1, 2, 100), ("Green", 2, 2, 110), ("Red", 3, 1, 15)]
    assert_presentation_matches(race, public(race))


def test_resolver_without_hidden_submissions_finishes_immediately(race):
    set_mode(race, "resolver")
    did = race["divisions"][0]
    with store._session() as db:
        for name in ("Blue", "Green"):
            db.get(SubmissionRow, race["submissions"][did][(name, "B")]).submitted_at = race["start"] + timedelta(minutes=55)
        db.commit()
    scores = visible_scores(public(race))
    completed = release(race, "start")
    assert completed["mode"] == "all"
    assert completed["resolver"]["total_steps"] == 0
    assert visible_scores(public(race)) == scores


def test_manual_freeze_before_scheduled_time_keeps_its_original_cutoff(race, monkeypatch):
    import importlib
    store_module = importlib.import_module("app.services.store")
    tick = now_utc()
    did = race["divisions"][0]
    store.update_contest_settings(race["cid"], status=ContestStatus.RUNNING,
        freeze_at=tick + timedelta(minutes=30), end_at=tick + timedelta(hours=1))
    with store._session() as db:
        db.get(SubmissionRow, race["submissions"][did][("Blue", "B")]).submitted_at = tick + timedelta(minutes=1)
        db.commit()
    monkeypatch.setattr(store_module, "now_utc", lambda: tick)
    response = client.patch(settings_url(race), headers=race["headers"], json={"scoreboard_freeze_mode": "frozen"})
    assert response.status_code == 200, response.text
    before = public(race)
    assert before["frozen"] is True
    assert next(row for row in before["rows"] if row["team_name"] == "Blue")["solved"] == 1
    monkeypatch.setattr(store_module, "now_utc", lambda: tick + timedelta(minutes=2))
    assert public(race) == before
    live = client.patch(settings_url(race), headers=race["headers"], json={"scoreboard_freeze_mode": "live"})
    assert live.status_code == 200
    assert next(row for row in public(race)["rows"] if row["team_name"] == "Blue")["solved"] == 2


def test_release_modes_migration_preserves_existing_manual_snapshots():
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "migrations/versions/0027_scoreboard_release_modes.py"
    spec = importlib.util.spec_from_file_location("release_modes_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY, "
            "scoreboard_freeze_mode VARCHAR(32) DEFAULT 'auto')"))
        connection.execute(sa.text("CREATE TABLE scoreboard_releases (division_id VARCHAR(36) PRIMARY KEY, "
            "contest_id VARCHAR(36), mode VARCHAR(16), snapshot_rows JSON, revealed_ranks JSON, created_at DATETIME)"))
        connection.execute(sa.text("INSERT INTO contests (contest_id) VALUES ('contest')"))
        connection.execute(sa.text("INSERT INTO contests (contest_id, scoreboard_freeze_mode) VALUES ('frozen', 'frozen')"))
        connection.execute(sa.text("INSERT INTO scoreboard_releases (division_id, contest_id, mode, snapshot_rows, revealed_ranks) "
            "VALUES ('division', 'contest', 'partial', :snapshot, '[2]')"),
            {"snapshot": '[{"rank":2,"team_name":"Keep me"}]'})
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        contest = connection.execute(sa.text("SELECT scoreboard_release_mode, scoreboard_frozen_at FROM contests WHERE contest_id = 'contest'")).one()
        assert contest == ("manual", None)
        assert connection.scalar(sa.text("SELECT scoreboard_frozen_at FROM contests WHERE contest_id = 'frozen'")) is not None
        release_state = connection.execute(sa.text("SELECT mode, strategy, snapshot_rows, revealed_ranks, resolver_state FROM scoreboard_releases")).one()
        assert release_state == ("partial", "manual", '[{"rank":2,"team_name":"Keep me"}]', "[2]", None)
        migration.downgrade()
        assert connection.scalar(sa.text("SELECT revealed_ranks FROM scoreboard_releases")) == "[2]"
        assert "scoreboard_release_mode" not in {column["name"] for column in sa.inspect(connection).get_columns("contests")}
