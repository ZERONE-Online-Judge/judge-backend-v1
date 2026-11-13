"""Bulk publication stays frozen until an operator explicitly releases live results."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder

from app.models import Contest, ContestStatus, now_utc
from app.orm_models import ContestRow, ScoreboardReleaseRow, SubmissionRow, TeamMemberRow
from app.services.scoreboard_events import reveal_versions
from app.services.store import store
from tests.contest_fixtures import set_contest_schedule
from test_scoreboard_release_modes import (
    race, client, public, release, release_url, set_mode, visible_scores,
    assert_presentation_matches,
)
from test_staff_scoreboard import staff, endpoint, rows as staff_rows


@pytest.fixture
def bulk(race):
    set_mode(race, "manual_all")
    store.update_contest_settings(race["cid"], submission_access_after_end="public")
    return race


def submissions(bulk, **params):
    response = client.get(f"/api/contests/{bulk['cid']}/submissions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_new_contests_default_to_resolver_in_models_orm_and_service():
    created = store.create_contest("Resolver default", "Host", "")
    assert created.scoreboard_release_mode == "resolver"
    model_fields = created.model_dump()
    del model_fields["scoreboard_release_mode"]
    assert Contest.model_validate(model_fields).scoreboard_release_mode == "resolver"
    assert str(ContestRow.__table__.c.scoreboard_release_mode.server_default.arg) == "resolver"


@pytest.mark.parametrize("freeze_mode", ["auto", "live", "frozen"])
@pytest.mark.parametrize("counts", [False, True])
def test_ended_bulk_hold_protects_public_participant_staff_and_presentation(bulk, freeze_mode, counts):
    store.update_contest_settings(bulk["cid"], scoreboard_freeze_mode=freeze_mode,
        freeze_submission_count_visible=counts)
    did = bulk["divisions"][0]
    before = public(bulk)
    assert before["frozen"] is True
    assert visible_scores(before) == [("Red", 1, 1, 15), ("Blue", 2, 1, 20), ("Green", 3, 1, 40)]
    assert before["release"] == {"mode": "not_started", "strategy": "manual_all", "ranks": [],
        "revealed_count": 0, "total_count": 3}
    for row in before["rows"]:
        score = next(item for item in row["problem_scores"] if item["problem_code"] == "B")
        assert score.get("pending_attempts", 0) == (1 if counts and row["team_name"] != "Red" else 0)
    assert "SECRET SOURCE" not in str(before)
    assert_presentation_matches(bulk, before)
    _, basic = staff(bulk)
    assert staff_rows(client.get(endpoint(bulk), headers=basic).json()["data"]) == before["rows"]
    email = f"bulk-{uuid4().hex}@zoj.com"
    with store._session() as db:
        db.add(TeamMemberRow(contest_id=bulk["cid"], participant_team_id=bulk["teams"][did]["Blue"],
            name="Member", email=email, role="leader"))
        db.commit()
    auth = client.post(f"/api/contests/{bulk['cid']}/participant-login/otp/verify",
        json={"email": email, "otp_code": "", "force_new_session": True})
    assert auth.status_code == 200, auth.text
    participant = client.get(f"/api/contests/{bulk['cid']}/divisions/{did}/scoreboard",
        headers={"Authorization": "Bearer " + auth.json()["data"]["access_token"]})
    assert participant.status_code == 200, participant.text
    assert participant.json()["data"]["rows"] == before["rows"]
    listed = submissions(bulk, division_id=did, limit=1)
    assert listed["page"]["total_count"] == 3
    hidden_ids = {bulk["submissions"][did][(name, "B")] for name in ("Blue", "Green")}
    assert not hidden_ids.intersection(item["submission_id"] for item in listed["data"])
    if not counts:
        with store._session() as db:
            db.get(SubmissionRow, bulk["submissions"][did][("Blue", "B")]).status = "wrong_answer"
            db.commit()
        assert public(bulk) == before
        assert submissions(bulk, division_id=did, limit=1)["page"] == listed["page"]


@pytest.mark.parametrize("action", ["all", "undo"])
def test_bulk_actions_refuse_before_contest_end(bulk, action):
    set_contest_schedule(bulk["cid"], status=ContestStatus.RUNNING, end_at=now_utc() + timedelta(hours=1))
    response = client.post(release_url(bulk), headers=bulk["headers"], json={"action": action, "expected_revision": 0})
    assert response.status_code == 409


def test_live_override_before_end_does_not_skip_the_ended_bulk_hold(bulk):
    set_contest_schedule(bulk["cid"], scoreboard_freeze_mode="live",
        status=ContestStatus.RUNNING, end_at=now_utc() + timedelta(hours=1))
    running = public(bulk)
    assert not running["frozen"] and max(row["solved"] for row in running["rows"]) == 2
    store.update_contest_settings(bulk["cid"], status=ContestStatus.ENDED,
        end_at=now_utc() - timedelta(minutes=1))
    ended = public(bulk)
    assert ended["frozen"] and all(row["solved"] == 1 for row in ended["rows"])
    release(bulk, "all")
    assert public(bulk)["rows"] == running["rows"]


@pytest.mark.parametrize("action", ["start", "rank", "next"])
def test_bulk_has_no_start_rank_or_next_action(bulk, action):
    response = client.post(release_url(bulk), headers=bulk["headers"], json={"action": action, "rank": 1})
    assert response.status_code == 409


def test_bulk_all_tracks_finishing_judges_and_rejudging_like_immediate(bulk):
    did, other = bulk["divisions"]
    untouched = public(bulk, other)
    with store._session() as db:
        db.get(SubmissionRow, bulk["submissions"][did][("Green", "B")]).status = "judging"
        db.commit()
    before_version = reveal_versions([bulk["cid"]])
    published = release(bulk, "all", expected_revision=0)
    assert published == {"mode": "all", "strategy": "manual_all", "ranks": [], "revealed_count": 3,
        "total_count": 3, "revision": 1, "undo": {"action": "all"}}
    assert reveal_versions([bulk["cid"]]) != before_version
    assert public(bulk)["frozen"] is False
    with store._session() as db:
        saved = db.get(ScoreboardReleaseRow, did)
        assert saved.snapshot_rows == [] and saved.resolver_state is None
        db.get(SubmissionRow, bulk["submissions"][did][("Green", "B")]).status = "accepted"
        db.get(SubmissionRow, bulk["submissions"][did][("Blue", "B")]).status = "wrong_answer"
        db.commit()
    board = public(bulk)
    internal = jsonable_encoder(store.scoreboard_rows(bulk["cid"], did, public_view=False))
    assert visible_scores(board) == visible_scores(internal)
    assert all("rank_is_final" not in row for row in board["rows"])
    assert public(bulk, other) == untouched
    assert submissions(bulk, division_id=did)["page"]["total_count"] == 5
    assert_presentation_matches(bulk, board)


def test_bulk_undo_restores_hold_and_can_republish_without_stale_duplicates(bulk):
    store.update_contest_settings(bulk["cid"], scoreboard_freeze_mode="live", freeze_submission_count_visible=False)
    before = public(bulk)
    for body in ({"action": "undo"}, {"action": "undo", "expected_revision": 0}):
        assert client.post(release_url(bulk), headers=bulk["headers"], json=body).status_code == 409
    first = release(bulk, "all", expected_revision=0)
    assert release(bulk, "all")["revision"] == first["revision"]
    stale = client.post(release_url(bulk), headers=bulk["headers"], json={"action": "all", "expected_revision": 0})
    assert stale.status_code == 409
    undone = release(bulk, "undo", expected_revision=first["revision"])
    assert undone["mode"] == "not_started" and undone["strategy"] == "manual_all"
    assert undone["revision"] == 2 and undone["undo"] is None and undone["revealed_count"] == 0
    assert public(bulk) == before
    assert submissions(bulk, division_id=bulk["divisions"][0])["page"]["total_count"] == 3
    assert client.post(release_url(bulk), headers=bulk["headers"], json={"action": "undo", "expected_revision": 1}).status_code == 409
    assert release(bulk, "all", expected_revision=2)["revision"] == 3


def test_bulk_aggregate_composes_only_published_divisions_and_tracks_undo(bulk):
    store.update_contest_settings(bulk["cid"], scoreboard_freeze_mode="live", freeze_submission_count_visible=True)
    did, other = bulk["divisions"]
    _, basic = staff(bulk)
    before = store.scoreboard_rows(bulk["cid"], public_view=True)
    assert before["frozen"] and all(row["solved"] == 1 for row in before["rows"])
    first = release(bulk, "all")
    mixed = store.scoreboard_rows(bulk["cid"], public_view=True)
    assert mixed["frozen"] and mixed["release"]["mode"] == "partial"
    assert mixed["release"]["revealed_count"] == 3 and mixed["release"]["total_count"] == 6
    for row in mixed["rows"]:
        if row["division_id"] == did:
            assert not any(score.get("pending_attempts", 0) for score in row["problem_scores"])
        else:
            assert row["solved"] == 1
    served = client.get(endpoint(bulk, division=False), headers=basic)
    assert served.status_code == 200
    assert staff_rows(served.json()["data"]) == jsonable_encoder(mixed["rows"])
    assert submissions(bulk)["page"]["total_count"] == 8
    release(bulk, "undo", expected_revision=first["revision"])
    assert store.scoreboard_rows(bulk["cid"], public_view=True) == before
    release(bulk, "all")
    response = client.post(release_url(bulk, other), headers=bulk["headers"], json={"action": "all"})
    assert response.status_code == 200
    complete = store.scoreboard_rows(bulk["cid"], public_view=True)
    assert not complete["frozen"] and complete["release"]["mode"] == "all"
    assert complete["release"]["revealed_count"] == complete["release"]["total_count"] == 6


def test_bulk_duplicate_requests_are_serialized_by_the_contest_row(bulk):
    with store._session() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Requires real PostgreSQL row locks")
    def publish():
        return client.post(release_url(bulk), headers=bulk["headers"],
            json={"action": "all", "expected_revision": 0}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: publish(), range(2))) == [200, 409]
    assert store.scoreboard_release(bulk["cid"], bulk["divisions"][0])["revision"] == 1
