"""Rank announcements publish fixed final positions, never mixed-score ranks."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder

from app.models import ContestStatus, now_utc
from app.orm_models import (
    ParticipantTeamRow, ProblemRow, ScoreboardReleaseRow, SubmissionRow,
    TeamMemberRow,
)
from app.services.store import DbStore, store
from test_scoreboard_release_modes import (
    assert_presentation_matches, client, login, public, release, release_url,
    set_mode,
)


@pytest.fixture
def ceremony():
    start = now_utc() - timedelta(hours=3)
    contest = store.create_contest("Final rank announcements", "Test", "",
        start_at=start, freeze_at=start + timedelta(minutes=60),
        end_at=start + timedelta(minutes=120), status=ContestStatus.ENDED)
    store.update_contest_settings(contest.contest_id,
        scoreboard_access_after_end="public", freeze_submission_count_visible=True)
    division = store.create_contest_division(contest.contest_id, "A", "A")
    teams, submissions = {}, {}
    with store._session() as db:
        problems = {}
        for index, code in enumerate("ABCDE", start=1):
            problem = ProblemRow(contest_id=contest.contest_id,
                division_id=division.division_id, problem_code=code, title=code,
                statement="", time_limit_ms=1000, memory_limit_mb=128,
                display_order=index)
            db.add(problem)
            db.flush()
            problems[code] = problem.problem_id
        # Echo is frozen rank 2 but final rank 5. Revealing its final score must
        # not call it second while the stronger teams remain frozen.
        for name, first, later in (
            ("Alpha", 1, [70, 80, 90, 100]),
            ("Bravo", 20, [72, 82, 92]),
            ("Charlie", 30, [74, 84]),
            ("Delta", 40, [76]),
            ("Echo", 5, []),
            ("Foxtrot", 50, []),
        ):
            team = ParticipantTeamRow(contest_id=contest.contest_id,
                division_id=division.division_id, team_name=name)
            db.add(team)
            db.flush()
            teams[name] = team.participant_team_id
            for code, minute in zip("ABCDE", [first, *later]):
                submission = SubmissionRow(contest_id=contest.contest_id,
                    division_id=division.division_id, problem_id=problems[code],
                    participant_team_id=team.participant_team_id, language="cpp17",
                    source_code="UNPUBLISHED SOURCE", status="accepted",
                    submitted_at=start + timedelta(minutes=minute))
                db.add(submission)
                db.flush()
                submissions[(name, code)] = submission.submission_id
        participant_email = f"rank-participant-{uuid4().hex}@zoj.com"
        db.add(TeamMemberRow(contest_id=contest.contest_id,
            participant_team_id=teams["Echo"], name="Participant",
            email=participant_email, role="leader"))
        db.commit()
    return {"cid": contest.contest_id, "divisions": [division.division_id],
        "teams": teams, "submissions": submissions, "start": start,
        "participant_email": participant_email, "headers": login("test3@zoj.com")}


def by_name(board):
    return {row["team_name"]: row for row in board["rows"]}


def announced(board):
    return [row for row in board["rows"] if row["rank_is_final"]]


def undo(ceremony):
    revision = client.get(release_url(ceremony),
        headers=ceremony["headers"]).json()["data"]["revision"]
    return release(ceremony, "undo", expected_revision=revision)


def test_fifth_place_announcement_keeps_fifth_despite_frozen_second(ceremony):
    frozen = public(ceremony)
    assert by_name(frozen)["Echo"]["rank"] == 2
    assert all("rank_is_final" not in row for row in frozen["rows"])
    release(ceremony, "start")
    release(ceremony, "rank", rank=5)
    board = public(ceremony)
    assert [(row["team_name"], row["rank"]) for row in announced(board)] == [("Echo", 5)]
    assert board["rows"][0]["team_name"] == "Echo"
    hidden = [row for row in board["rows"] if not row["rank_is_final"]]
    assert [(row["team_name"], row["rank"]) for row in hidden] == [
        (row["team_name"], row["rank"]) for row in frozen["rows"]
        if row["team_name"] != "Echo"
    ]
    assert all(row["solved"] == 1 for row in hidden)
    # Result completeness does not itself announce a final rank: Foxtrot has
    # no hidden submissions but its sixth-place identity is still unpublished.
    assert by_name(board)["Foxtrot"]["is_finalized"] is True
    assert by_name(board)["Foxtrot"]["rank_is_final"] is False
    assert board["release"]["ranks"] == []
    assert_presentation_matches(ceremony, board)


@pytest.mark.parametrize("order", [(6, 5, 4, 3, 2), (5, 2, 6, 4, 3)])
def test_each_announcement_keeps_existing_final_ranks_in_any_order(ceremony, order):
    frozen = public(ceremony)
    release(ceremony, "start")
    expected_names = {rank: name for rank, name in enumerate(
        ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"), start=1)}
    seen = set()
    for rank in order:
        release(ceremony, "rank", rank=rank)
        seen.add(rank)
        board = public(ceremony)
        assert [(row["team_name"], row["rank"]) for row in announced(board)] == [
            (expected_names[number], number) for number in sorted(seen)
        ]
        assert [row["team_name"] for row in board["rows"] if not row["rank_is_final"]] == [
            row["team_name"] for row in frozen["rows"]
            if row["team_name"] not in {expected_names[number] for number in seen}
        ]
    release(ceremony, "rank", rank=1)
    complete = public(ceremony)
    assert complete["release"]["mode"] == "all"
    assert [row["rank"] for row in complete["rows"]] == [1, 2, 3, 4, 5, 6]
    assert all("rank_is_final" not in row for row in complete["rows"])


def test_tied_final_rank_is_atomic_and_survives_resume_and_undo(ceremony):
    with store._session() as db:
        db.get(SubmissionRow, ceremony["submissions"][("Foxtrot", "A")]).submitted_at = (
            ceremony["start"] + timedelta(minutes=5))
        db.commit()
    release(ceremony, "start")
    initial = public(ceremony)
    result = release(ceremony, "rank", rank=5)
    tied = public(ceremony)
    assert result["revealed_count"] == 2
    assert [(row["team_name"], row["rank"]) for row in announced(tied)] == [
        ("Echo", 5), ("Foxtrot", 5)]
    assert release(ceremony, "start") == result
    assert client.get(release_url(ceremony), headers=ceremony["headers"]).json()["data"] == result
    assert jsonable_encoder(DbStore().scoreboard_rows(ceremony["cid"],
        ceremony["divisions"][0], public_view=True)) == {
            key: value for key, value in tied.items() if key != "division"}
    # Rejudging cannot alter an already captured ceremony or its inverse.
    with store._session() as db:
        db.get(SubmissionRow, ceremony["submissions"][("Echo", "A")]).status = "wrong_answer"
        db.commit()
    assert public(ceremony) == tied
    assert undo(ceremony)["revealed_count"] == 0
    assert public(ceremony) == initial
    release(ceremony, "rank", rank=5)
    assert public(ceremony) == tied
    release(ceremony, "all")
    assert undo(ceremony)["mode"] == "partial"
    assert public(ceremony) == tied


@pytest.mark.parametrize("counts", [False, True])
def test_hidden_final_rank_order_ties_and_scores_never_change_public_payload(ceremony, counts):
    store.update_contest_settings(ceremony["cid"], freeze_submission_count_visible=counts)
    release(ceremony, "start")
    release(ceremony, "rank", rank=5)
    before = public(ceremony)
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, ceremony["divisions"][0])
        altered = deepcopy(saved.snapshot_rows)
        hidden = [row for row in altered if row["rank"] != 5]
        for index, row in enumerate(hidden):
            row["rank"] = [4, 3, 2, 1, 1][index]
            row["solved"] += index + 7
            row["penalty"] += 700 + index
            row["submission_count"] += 500 + index
        saved.snapshot_rows = list(reversed(altered))
        saved.revision += 1
        db.commit()
    assert public(ceremony) == before
    assert_presentation_matches(ceremony, before)
    assert before["release"]["ranks"] == []
    assert "snapshot_rows" not in str(before)
    assert "UNPUBLISHED SOURCE" not in str(before)


def test_public_participant_basic_staff_and_presentation_share_fixed_ranks(ceremony):
    release(ceremony, "start")
    release(ceremony, "rank", rank=5)
    expected = public(ceremony)
    team_login = store.verify_otp(ceremony["cid"], ceremony["participant_email"], "")
    assert team_login is not None
    participant_headers = {"Authorization": "Bearer " + team_login[3]}
    base = f"/api/contests/{ceremony['cid']}"
    did = ceremony["divisions"][0]
    for path in (base + "/scoreboard", base + f"/divisions/{did}/scoreboard"):
        response = client.get(path, headers=participant_headers)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["rows"] == expected["rows"]
    store.upsert_contest_operator(ceremony["cid"], f"owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    email = f"basic-{uuid4().hex}@zoj.com"
    store.upsert_contest_operator(ceremony["cid"], email, "Basic", ["notices_manager"])
    internal_path = f"/api/operator/contests/{ceremony['cid']}/divisions/{did}/scoreboard/internal"
    basic = client.get(internal_path, headers=login(email)).json()["data"]
    assert basic["operator_live_view"] is False
    assert [{key: value for key, value in row.items() if key != "visible_to_team"}
        for row in basic["rows"]] == expected["rows"]
    actual = client.get(internal_path, headers=ceremony["headers"]).json()["data"]
    assert actual["operator_live_view"] is True
    assert [(row["team_name"], row["rank"]) for row in actual["rows"]] == [
        (name, index) for index, name in enumerate(
            ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"), start=1)]
    assert all("rank_is_final" not in row for row in actual["rows"])
    assert_presentation_matches(ceremony, expected)
    store.update_contest_settings(ceremony["cid"], scoreboard_access_after_end="participants")
    assert client.get(base + f"/divisions/{did}/scoreboard").status_code == 404
    assert client.get(base + f"/divisions/{did}/scoreboard",
        headers=participant_headers).json()["data"]["rows"] == expected["rows"]
    store.update_contest_settings(ceremony["cid"], scoreboard_access_after_end="private")
    assert client.get(base + f"/divisions/{did}/scoreboard",
        headers=participant_headers).status_code == 404


@pytest.mark.parametrize("mode", ["resolver", "immediate"])
def test_rank_confirmation_metadata_does_not_change_other_release_modes(ceremony, mode):
    set_mode(ceremony, mode)
    assert all("rank_is_final" not in row for row in public(ceremony)["rows"])
    if mode == "resolver":
        release(ceremony, "start")
        release(ceremony, "next")
        assert all("rank_is_final" not in row for row in public(ceremony)["rows"])
        release(ceremony, "all")
    assert all("rank_is_final" not in row for row in public(ceremony)["rows"])
