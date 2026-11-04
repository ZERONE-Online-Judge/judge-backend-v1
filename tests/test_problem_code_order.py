"""Problem lists and saved announcements share ordinal code ordering."""
import os
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import ScoreboardReleaseRow, SubmissionRow
from app.services.problem_order import PROBLEM_ORDER_POLICY, problem_code_sort_key
from app.services.store import store
from app.settings import settings
from test_editor_bridge import KEY, headers as bridge_headers


client = TestClient(app)
EXPECTED = ["A1", "A10", "A2", "B"]


def login(email):
    response = client.post("/api/auth/general/otp/verify", json={
        "email": email, "otp_code": "", "force_new_session": True,
    })
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.fixture
def contest():
    start = now_utc() - timedelta(hours=3)
    value = store.create_contest("Problem code order", "Host", "", start_at=start,
        end_at=start + timedelta(hours=2), freeze_at=start + timedelta(hours=1), status=ContestStatus.ENDED)
    cid = value.contest_id
    division = store.create_contest_division(cid, "main", "Main")
    email = f"order-member-{uuid4().hex}@example.com"
    team = store.create_participant_team(cid, division.division_id, "Team", "Member", email, [])
    ids = {}
    for order, code in enumerate(("A2", "B", "A10", "A1"), start=1):
        problem = store.create_problem(cid, division.division_id, code, f"Title {4-order}", "", 1000, 128, {}, order)
        ids[code] = problem.problem_id
        with store._session() as db:
            db.add(SubmissionRow(contest_id=cid, division_id=division.division_id,
                problem_id=problem.problem_id, participant_team_id=team.participant_team_id,
                language="cpp17", source_code="fixture only", status="accepted",
                submitted_at=start + timedelta(minutes=70 + order)))
            db.commit()
    store.update_contest_settings(cid, problem_access_after_end="public", scoreboard_access_after_end="public",
        freeze_submission_count_visible=True)
    return {"cid": cid, "did": division.division_id, "email": email, "ids": ids}


def codes(items):
    return [item["problem_code"] for item in items]


def test_sort_key_is_ordinal_and_ignores_titles_and_manual_display_order():
    values = [
        {"problem_code": "A2", "problem_id": "first", "display_order": 1},
        {"problem_code": "A10", "problem_id": "second", "display_order": 2},
        {"problem_code": "A1", "problem_id": "z", "title": "First"},
        {"problem_code": "A1", "problem_id": "a", "title": "Last"},
        {"problem_code": "a", "problem_id": "lower"},
        {"problem_code": "Z", "problem_id": "upper"},
        {"problem_code": "\U0001f600", "problem_id": "emoji"},
        {"problem_code": "\U00010000", "problem_id": "supplementary"},
        {"problem_code": "가", "problem_id": "korean"},
    ]
    expected = ["a", "z", "second", "first", "upper", "lower", "korean", "supplementary", "emoji"]
    assert [item["problem_id"] for item in sorted(values, key=problem_code_sort_key)] == expected
    assert [item.problem_id for item in sorted([SimpleNamespace(**value) for value in values],
        key=problem_code_sort_key)] == expected


@pytest.mark.parametrize("authenticated", [False, True])
def test_anonymous_and_participant_lists_and_workspaces_share_code_order(contest, authenticated):
    headers = {"Authorization": "Bearer " + login(contest["email"])["access_token"]} if authenticated else {}
    base = f"/api/contests/{contest['cid']}"
    for path in ("/problems", f"/divisions/{contest['did']}/problems", "/workspace", f"/divisions/{contest['did']}/workspace"):
        response = client.get(base + path, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert codes(data if isinstance(data, list) else data["problems"]) == EXPECTED
    assert [item.problem_code for item in store.contest_problems(contest["cid"], contest["did"])] == EXPECTED


def test_operator_lists_filters_and_presentation_share_code_order(contest):
    headers = {"Authorization": "Bearer " + login("test3@zoj.com")["operator_session"]["access_token"]}
    base = f"/api/operator/contests/{contest['cid']}"
    problems = client.get(base + "/problems", headers=headers)
    assert problems.status_code == 200, problems.text
    assert codes(problems.json()["data"]) == EXPECTED
    filters = client.get(base + "/submission-filters", headers=headers)
    assert filters.status_code == 200, filters.text
    assert codes(filters.json()["data"]["problems"]) == EXPECTED
    presentation = client.get(base + "/scoreboard/presentation", headers=headers)
    assert presentation.status_code == 200, presentation.text
    section = presentation.json()["data"]["sections"][0]
    assert codes(section["problems"]) == EXPECTED
    assert all(codes(row["problem_scores"]) == EXPECTED for row in section["rows"])


def test_editor_import_target_list_uses_the_same_code_order(contest, monkeypatch):
    monkeypatch.setattr(settings, "editor_bridge_secret", SecretStr(KEY))
    path = f"/api/editor/contests/{contest['cid']}/problems"
    response = client.get(path, headers=bridge_headers("GET", path, "test3@zoj.com"))
    assert response.status_code == 200, response.text
    assert [item["code"] for item in response.json()["data"]["problems"]] == EXPECTED


@pytest.mark.parametrize("public_view", [False, True])
@pytest.mark.parametrize("stage", ["live", "frozen", "ended"])
def test_live_and_frozen_scoreboards_have_identical_code_order(contest, public_view, stage):
    if stage != "ended":
        changes = {"status": ContestStatus.RUNNING, "end_at": now_utc() + timedelta(hours=1)}
        if stage == "live":
            changes["freeze_at"] = now_utc() + timedelta(minutes=20)
        store.update_contest_settings(contest["cid"], **changes)
    board = store.scoreboard_rows(contest["cid"], contest["did"], public_view=public_view)
    assert codes(board["rows"][0]["problem_scores"]) == EXPECTED
    hidden = public_view and stage != "live"
    assert board["rows"][0]["solved"] == (0 if hidden else 4)
    if hidden:
        assert all(not score["solved"] for score in board["rows"][0]["problem_scores"])


@pytest.mark.parametrize("strategy", ["manual", "resolver"])
@pytest.mark.parametrize("mode", ["partial", "all"])
def test_saved_release_scores_are_sorted_only_in_the_public_projection(contest, strategy, mode):
    store.update_contest_settings(contest["cid"], scoreboard_release_mode=strategy)
    store.update_scoreboard_release(contest["cid"], contest["did"], "start")
    with store._session() as db:
        release = db.get(ScoreboardReleaseRow, contest["did"])
        snapshots = deepcopy(release.snapshot_rows)
        state = deepcopy(release.resolver_state)
        for row in snapshots + state["rows"]:
            row["problem_scores"].reverse()
        release.snapshot_rows, release.resolver_state, release.mode = snapshots, state, mode
        db.commit()
        stored = (deepcopy(snapshots), deepcopy(state))
    board = store.scoreboard_rows(contest["cid"], contest["did"], public_view=True)
    assert codes(board["rows"][0]["problem_scores"]) == EXPECTED
    assert board["rows"][0]["solved"] == (4 if mode == "all" else 0)
    with store._session() as db:
        release = db.get(ScoreboardReleaseRow, contest["did"])
        assert (release.snapshot_rows, release.resolver_state) == stored


def test_existing_resolver_uses_code_order_and_undo_preserves_saved_cell_identity(contest):
    store.update_contest_settings(contest["cid"], scoreboard_release_mode="resolver")
    store.update_scoreboard_release(contest["cid"], contest["did"], "start")
    with store._session() as db:
        release = db.get(ScoreboardReleaseRow, contest["did"])
        state = deepcopy(release.resolver_state)
        state["rows"][0]["problem_scores"].reverse()
        release.resolver_state = state
        before = deepcopy(state)
        inverse = store._advance_resolver(release)
        assert release.resolver_state["last_event"]["problem_code"] == "A1"
        assert release.resolver_state["rows"][0]["solved"] == 1
        assert inverse["row"] == before["rows"][0]
        release.undo_history = [{"action": "next", "mode": "partial", "revealed_ranks": [], "resolver_delta": inverse}]
        store._undo_scoreboard_release(release)
        assert release.resolver_state == before
        assert release.resolver_state["rows"][0]["problem_scores"][0]["problem_code"] == "B"


def test_order_policy_changes_the_shared_scoreboard_cache_identity(contest, monkeypatch):
    seen = []
    def cached(group, identity, ttl, compute, **kwargs):
        seen.append(identity)
        return compute()
    monkeypatch.setattr("app.services.store.cached_result", cached)
    store.scoreboard_rows(contest["cid"], contest["did"], public_view=True)
    assert seen[0][0] == PROBLEM_ORDER_POLICY
