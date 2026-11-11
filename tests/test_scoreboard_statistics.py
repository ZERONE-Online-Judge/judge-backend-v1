"""Team acceptance rates count submitting teams once and remain internal."""
from datetime import timedelta

import pytest

from test_scoreboard_release_modes import race, client, public, release, set_mode
from app.models import ContestStatus, now_utc
from app.orm_models import ParticipantTeamRow, ProblemRow, SubmissionRow
from app.services.store import OPERATOR_TEST_TEAM_PREFIX, store


def problem_id(race, division=None):
    did = division or race["divisions"][0]
    with store._session() as db:
        return db.get(SubmissionRow, race["submissions"][did][("Blue", "B")]).problem_id


def submit(race, problem, team, status, *, division=None, kind="participant", minute=90):
    with store._session() as db:
        db.add(SubmissionRow(contest_id=race["cid"], division_id=division or race["divisions"][0],
            problem_id=problem, participant_team_id=team, submission_kind=kind,
            language="cpp17", source_code="", status=status,
            submitted_at=race["start"] + timedelta(minutes=minute)))
        db.commit()


def stat(race, problem, *, division=None):
    suffix = f"/divisions/{division}" if division else ""
    response = client.get(f"/api/operator/contests/{race['cid']}{suffix}/scoreboard/internal", headers=race["headers"])
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["data"]["problem_stats"] if item["problem_id"] == problem)


def test_team_rate_counts_each_attempting_and_accepted_team_once(race):
    did = race["divisions"][0]
    problem = problem_id(race)
    initial = stat(race, problem, division=did)
    assert initial["total_team_count"] == 3
    assert initial["attempted_team_count"] == 2
    assert initial["accepted_team_count"] == 2
    assert initial["team_acceptance_rate"] == 100.0

    for _ in range(3):
        submit(race, problem, race["teams"][did]["Red"], "wrong_answer")
    unsuccessful = stat(race, problem, division=did)
    assert unsuccessful["attempted_team_count"] == 3
    assert unsuccessful["accepted_team_count"] == 2
    assert unsuccessful["team_acceptance_rate"] == 66.7
    assert unsuccessful["acceptance_rate"] == 40.0

    submit(race, problem, race["teams"][did]["Blue"], "accepted")
    submit(race, problem, race["teams"][did]["Blue"], "wrong_answer")
    repeated = stat(race, problem, division=did)
    assert repeated["attempted_team_count"] == 3
    assert repeated["accepted_team_count"] == 2
    assert repeated["team_acceptance_rate"] == 66.7
    assert repeated["total_submissions"] == 7
    assert repeated["accepted_submissions"] == 3
    assert repeated["acceptance_rate"] == 42.9

    submit(race, problem, race["teams"][did]["Red"], "accepted")
    solved = stat(race, problem, division=did)
    assert solved["attempted_team_count"] == 3
    assert solved["accepted_team_count"] == 3
    assert solved["team_acceptance_rate"] == 100.0
    assert solved["acceptance_rate"] == 50.0


def test_team_rate_distinguishes_no_submissions_from_no_accepted_teams(race):
    did = race["divisions"][0]
    with store._session() as db:
        problem = ProblemRow(contest_id=race["cid"], division_id=did, problem_code="C",
            title="Unattempted problem", statement="", time_limit_ms=1000,
            memory_limit_mb=128, display_order=3)
        db.add(problem)
        db.flush()
        pid = problem.problem_id
        db.commit()
    empty = stat(race, pid, division=did)
    assert empty["attempted_team_count"] == empty["accepted_team_count"] == 0
    assert empty["team_acceptance_rate"] is None
    assert empty["acceptance_rate"] is None

    for status in ("wrong_answer", "compile_error", "judging", "system_error"):
        submit(race, pid, race["teams"][did]["Red"], status)
    submit(race, pid, race["teams"][did]["Blue"], "waiting")
    attempted = stat(race, pid, division=did)
    assert attempted["attempted_team_count"] == 2
    assert attempted["accepted_team_count"] == 0
    assert attempted["team_acceptance_rate"] == 0.0
    assert attempted["total_submissions"] == 5
    assert attempted["acceptance_rate"] == 0.0


def test_team_rate_keeps_divisions_and_operator_submissions_separate(race):
    did, other = race["divisions"]
    problem, other_problem = problem_id(race), problem_id(race, other)
    submit(race, other_problem, race["teams"][other]["Red"], "wrong_answer", division=other)
    with store._session() as db:
        team = ParticipantTeamRow(contest_id=race["cid"], division_id=did,
            team_name=OPERATOR_TEST_TEAM_PREFIX + "Statistics")
        db.add(team)
        db.flush()
        operator_team = team.participant_team_id
        db.commit()
    submit(race, problem, operator_team, "accepted")
    submit(race, problem, race["teams"][did]["Red"], "accepted", kind="operator_test")
    submit(race, problem, race["teams"][did]["Red"], "accepted", kind="mock_judging")

    current = stat(race, problem, division=did)
    assert current["total_team_count"] == 3
    assert current["total_submissions"] == current["accepted_submissions"] == 2
    assert current["attempted_team_count"] == current["accepted_team_count"] == 2
    assert current["team_acceptance_rate"] == 100.0
    assert stat(race, other_problem, division=other)["team_acceptance_rate"] == 66.7
    assert stat(race, problem)["attempted_team_count"] == 2
    assert stat(race, other_problem)["attempted_team_count"] == 3


@pytest.mark.parametrize("mode", ["manual", "manual_all", "resolver", "immediate"])
def test_public_projections_keep_statistics_private_through_freeze_and_release(race, mode):
    set_mode(race, mode)
    did = race["divisions"][0]
    problem = problem_id(race)

    def check():
        board = public(race)
        assert "problem_stats" not in board
        response = client.get(f"/api/operator/contests/{race['cid']}/divisions/{did}/scoreboard/internal",
            headers=race["headers"], params={"view": "participant"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["problem_stats"] == []
        internal = stat(race, problem, division=did)
        assert internal["attempted_team_count"] == internal["accepted_team_count"] == 2
        assert internal["team_acceptance_rate"] == 100.0
        return board

    store.update_contest_settings(race["cid"], status=ContestStatus.RUNNING,
        end_at=now_utc() + timedelta(hours=1), freeze_at=now_utc() + timedelta(minutes=30))
    assert check()["frozen"] is False
    store.update_contest_settings(race["cid"], freeze_at=race["start"] + timedelta(hours=1))
    assert check()["frozen"] is True
    store.update_contest_settings(race["cid"], status=ContestStatus.ENDED, end_at=now_utc() - timedelta(minutes=1))
    check()
    if mode in {"manual", "resolver"}:
        release(race, "start")
        check()
        if mode == "manual":
            release(race, "rank", rank=1)
        else:
            release(race, "next")
        check()
    if mode != "immediate":
        release(race, "all")
    assert check()["frozen"] is False
    current = store.scoreboard_release(race["cid"], did)
    release(race, "undo", expected_revision=current["revision"])
    assert check()["frozen"] is True
