"""Basic staff see the participant projection, never the internal standings."""
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder

from test_scoreboard_release_modes import race, client, login, public, set_mode
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


@pytest.mark.parametrize("mode", ["manual", "resolver", "immediate"])
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
        assert client.get(endpoint(race, division), headers=token).status_code == 403
        assert client.get(endpoint(race, division)).status_code == 401
