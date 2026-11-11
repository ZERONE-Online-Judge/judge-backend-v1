"""Basic staff see the participant projection, never the internal standings."""
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder

from test_scoreboard_release_modes import race, client, login, public, release, set_mode
from app.models import ContestStatus, now_utc
from app.services.store import store


def staff(race, role="notices_manager"):
    store.upsert_contest_operator(race["cid"], f"owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    account = store.upsert_contest_operator(race["cid"], f"staff-board-{uuid4().hex}@zoj.com", "Staff", [role])
    return str(account.email), login(str(account.email))


def endpoint(race, division=True):
    suffix = f"/divisions/{race['divisions'][0]}" if division else ""
    return f"/api/operator/contests/{race['cid']}{suffix}/scoreboard/internal"


def rows(board):
    return [{key: value for key, value in row.items() if key != "visible_to_team"} for row in board["rows"]]


def assert_participant_projection(response, expected):
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["operator_live_view"] is False
    assert data["frozen_public_view"] == expected["frozen"]
    assert rows(data) == expected["rows"]
    assert data["problem_stats"] == expected.get("problem_stats", [])
    assert data["release"] == expected.get("release")
    return data


@pytest.mark.parametrize("mode", ["manual", "manual_all", "resolver", "immediate"])
@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("counts", [False, True])
def test_started_staff_receive_only_participant_scores_and_statistics(race, mode, ended, counts):
    set_mode(race, mode)
    store.update_contest_settings(race["cid"], status=ContestStatus.ENDED if ended else ContestStatus.RUNNING,
        end_at=now_utc() - timedelta(minutes=1) if ended else now_utc() + timedelta(hours=1),
        freeze_submission_count_visible=counts)
    _, token = staff(race)
    for division in (True, False):
        did = race["divisions"][0] if division else None
        expected = public(race) if division else jsonable_encoder(store.scoreboard_rows(race["cid"], public_view=True))
        response = client.get(endpoint(race, division), headers=token)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["operator_live_view"] is False
        assert data["frozen_public_view"] == expected["frozen"]
        assert rows(data) == expected["rows"]
        assert data["problem_stats"] == expected.get("problem_stats", [])
        assert data["release"] == expected.get("release")
        if not counts:
            assert "pending_attempts" not in response.text
        assert "SECRET SOURCE" not in response.text
        privileged = client.get(endpoint(race, division), headers=race["headers"]).json()["data"]
        assert privileged["operator_live_view"] is True
        assert rows(privileged) == jsonable_encoder(store.scoreboard_rows(race["cid"], did, public_view=False))["rows"]


@pytest.mark.parametrize("status", ["draft", "schedule_tbd", "open"])
def test_staff_cannot_read_before_start_but_explicit_viewers_can(race, status):
    future = now_utc() + timedelta(days=2)
    store.update_contest_settings(race["cid"], status=status, start_at=future,
        freeze_at=future + timedelta(hours=1), end_at=future + timedelta(hours=2))
    _, basic = staff(race)
    _, privileged = staff(race, "scoreboard_viewer")
    for division in (True, False):
        assert client.get(endpoint(race, division), headers=basic).status_code == 403
        assert client.get(endpoint(race, division), headers=privileged).status_code == 200
        for token in (basic, privileged):
            assert client.get(endpoint(race, division), params={"view": "participant"}, headers=token).status_code == 403


@pytest.mark.parametrize("division", [True, False])
def test_privileged_participant_view_uses_only_the_public_projection(race, monkeypatch, division):
    expected = public(race) if division else jsonable_encoder(store.scoreboard_rows(race["cid"], public_view=True))
    default_response = client.get(endpoint(race, division), headers=race["headers"])
    assert default_response.status_code == 200, default_response.text
    internal = default_response.json()["data"]
    assert internal["operator_live_view"] is True
    assert rows(internal) != expected["rows"]
    explicit = client.get(endpoint(race, division), params={"view": "operator"}, headers=race["headers"])
    assert explicit.json()["data"] == internal

    original = store.scoreboard_rows
    calls = []

    def public_only(*args, **kwargs):
        calls.append(kwargs.get("public_view"))
        assert kwargs.get("public_view") is True
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "scoreboard_rows", public_only)
    response = client.get(endpoint(race, division), params={"view": "participant"}, headers=race["headers"])
    assert_participant_projection(response, expected)
    assert calls == [True]
    assert "SECRET SOURCE" not in response.text


@pytest.mark.parametrize("mode", ["manual", "manual_all", "resolver", "immediate"])
def test_privileged_participant_view_follows_public_release_progress(race, mode):
    set_mode(race, mode)

    def check():
        expected = public(race)
        response = client.get(endpoint(race), params={"view": "participant"}, headers=race["headers"])
        return assert_participant_projection(response, expected)

    initial = check()
    if mode in {"manual", "resolver"}:
        assert initial["frozen_public_view"] is True
        release(race, "start")
        started = check()
        assert started["release"]["mode"] == "partial"
        if mode == "manual":
            release(race, "rank", rank=1)
        else:
            release(race, "next")
        partial = check()
        assert partial["release"]["mode"] == "partial"
        assert partial["rows"] != started["rows"]
    if mode != "immediate":
        release(race, "all")
    complete = check()
    assert complete["frozen_public_view"] is False
    assert complete["release"]["mode"] == "all"
    current = store.scoreboard_release(race["cid"], race["divisions"][0])
    release(race, "undo", expected_revision=current["revision"])
    assert check()["frozen_public_view"] is True


def test_basic_staff_cannot_force_internal_view_or_bypass_restricted_routes(race):
    _, token = staff(race)
    for division in (True, False):
        expected = public(race) if division else jsonable_encoder(store.scoreboard_rows(race["cid"], public_view=True))
        for view in ("operator", "participant"):
            response = client.get(endpoint(race, division), params={"view": view}, headers=token)
            assert_participant_projection(response, expected)
    prefix = f"/api/operator/contests/{race['cid']}"
    release_endpoint = prefix + f"/divisions/{race['divisions'][0]}/scoreboard/release?view=participant"
    assert client.get(release_endpoint, headers=token).status_code == 403
    assert client.post(release_endpoint, headers=token, json={"action": "all"}).status_code == 403
    assert client.get(prefix + "/scoreboard/presentation?view=participant", headers=token).status_code == 403


def test_scoreboard_view_rejects_unknown_values(race):
    for division in (True, False):
        response = client.get(endpoint(race, division), params={"view": "public"}, headers=race["headers"])
        assert response.status_code == 422, response.text


def test_view_permission_changes_projection_for_the_same_token_without_cache_leaks(race):
    email, token = staff(race, "scoreboard_viewer")
    assert client.get(endpoint(race), headers=token).json()["data"]["operator_live_view"] is True
    store.upsert_contest_operator(race["cid"], email, "Staff", ["notices_manager"])
    basic = client.get(endpoint(race), headers=token).json()["data"]
    assert basic["operator_live_view"] is False
    assert rows(basic) == public(race)["rows"]
    # Seeing the public board grants neither reveal controls nor presentation access.
    prefix = f"/api/operator/contests/{race['cid']}"
    release = prefix + f"/divisions/{race['divisions'][0]}/scoreboard/release"
    assert client.get(release, headers=token).status_code == 403
    assert client.post(release, headers=token, json={"action": "all"}).status_code == 403
    assert client.get(prefix + "/scoreboard/presentation", headers=token).status_code == 403
    store.upsert_contest_operator(race["cid"], email, "Staff", ["scoreboard_viewer"])
    assert client.get(endpoint(race), headers=token).json()["data"]["operator_live_view"] is True


def test_other_contest_staff_and_unauthenticated_visitors_are_rejected(race):
    another = store.create_contest("Other contest", "Test", "", start_at=now_utc() + timedelta(days=1))
    store.upsert_contest_operator(another.contest_id, f"other-owner-{uuid4().hex}@zoj.com", "Owner", ["master"])
    account = store.upsert_contest_operator(another.contest_id, f"outsider-{uuid4().hex}@zoj.com", "Other", ["notices_manager"])
    token = login(str(account.email))
    for division in (True, False):
        for params in ({}, {"view": "participant"}, {"view": "operator"}):
            assert client.get(endpoint(race, division), params=params, headers=token).status_code == 403
            assert client.get(endpoint(race, division), params=params).status_code == 401
