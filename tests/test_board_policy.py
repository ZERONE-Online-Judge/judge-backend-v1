"""Participant writing choices remain constrained by live contest policy."""

import importlib.util
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from sqlalchemy import create_engine, inspect, select, text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.database import SessionLocal
from app.orm_models import ContestRow
from app.services.store import store
from app.services.errors import AppError
from test_contest_visibility import context as visibility_context, client, login
from test_participant_preview import preview, select_division, headers


def participant_headers(cid, auth):
    result = client.post(
        f"/api/auth/general/contests/{cid}/participant-session", headers=auth
    )
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["data"]["access_token"]}


@pytest.fixture
def context(visibility_context):
    c = visibility_context
    c["general_member"] = c["member"]
    c["member"] = participant_headers(c["cid"], c["member"])
    return c


def board(c):
    return f"/api/contests/{c['cid']}/boards"


def settings(c, **values):
    result = client.patch(
        f"/api/operator/contests/{c['cid']}/settings", headers=c["owner"], json=values
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def post(c, **values):
    return client.post(
        board(c),
        headers=c["member"],
        json={"title": "Question", "body": "Question body", **values},
    )


def other_participant(c):
    email = f"another-{uuid4().hex}@zoj.com"
    store.create_participant_team(
        c["cid"], c["team"].division_id, "Other team", "Other member", email, []
    )
    return participant_headers(c["cid"], login(email))


def test_default_policy_preserves_legacy_choices(context):
    c = context
    result = post(c)
    assert result.status_code == 200, result.text
    question = result.json()["data"]
    assert (
        question["visibility"],
        question["comment_scope"],
        question["effective_comment_scope"],
    ) == ("public", "all", "all")
    assert (
        client.post(
            board(c) + f"/{question['contest_question_id']}/answers",
            headers=c["member"],
            json={"body": "Follow-up"},
        ).status_code
        == 200
    )


@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("scope", ["all", "operators"])
def test_selected_defaults_and_missing_old_client_fields(context, visibility, scope):
    c = context
    saved = settings(
        c,
        board_question_default_visibility=visibility,
        board_comment_default_scope=scope,
    )
    assert saved["board_question_default_visibility"] == visibility
    result = post(c)
    assert result.status_code == 200, result.text
    assert (
        result.json()["data"]["visibility"],
        result.json()["data"]["comment_scope"],
    ) == (visibility, scope)


@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("scope", ["all", "operators"])
def test_forced_policy_rejects_tampered_choices_and_normalizes_defaults(
    context, visibility, scope
):
    c = context
    saved = settings(
        c,
        board_question_visibility_policy=visibility,
        board_question_default_visibility=(
            "private" if visibility == "public" else "public"
        ),
        board_comment_scope_policy=scope,
        board_comment_default_scope="operators" if scope == "all" else "all",
    )
    assert saved["board_question_default_visibility"] == visibility
    assert saved["board_comment_default_scope"] == scope
    assert (
        post(
            c,
            visibility="private" if visibility == "public" else "public",
            comment_scope=scope,
        ).status_code
        == 403
    )
    assert (
        post(
            c,
            visibility=visibility,
            comment_scope="operators" if scope == "all" else "all",
        ).status_code
        == 403
    )
    assert store.questions_for_view(c["cid"], operator=True) == []
    assert post(c, visibility=visibility, comment_scope=scope).status_code == 200


def test_operators_only_question_rejects_author_and_other_participants_but_allows_staff(
    context,
):
    c = context
    result = post(c, comment_scope="operators")
    assert result.status_code == 200, result.text
    qid = result.json()["data"]["contest_question_id"]
    for auth in (c["member"], other_participant(c)):
        result = client.post(
            board(c) + f"/{qid}/answers", headers=auth, json={"body": "Not permitted"}
        )
        assert result.status_code == 403, result.text
        assert result.json()["error"]["code"] == "board_comments_operators_only"
    assert store.get_contest_question(c["cid"], qid).answers == []
    result = client.post(
        f"/api/operator/contests/{c['cid']}/boards/{qid}/answers",
        headers=c["owner"],
        json={"body": "Staff answer"},
    )
    assert result.status_code == 200, result.text


def test_live_contest_policy_overrides_existing_comment_scopes_without_revealing_private_posts(
    context,
):
    c = context
    q = post(c, visibility="private", comment_scope="all").json()["data"]
    qid = q["contest_question_id"]
    other = other_participant(c)
    settings(
        c,
        board_comment_scope_policy="operators",
        board_question_visibility_policy="public",
    )
    listed = client.get(board(c), headers=c["member"]).json()["data"][0]
    assert listed["visibility"] == "private"
    assert (
        listed["comment_scope"] == "all"
        and listed["effective_comment_scope"] == "operators"
    )
    assert client.get(board(c), headers=other).json()["data"] == []
    assert (
        client.post(
            board(c) + f"/{qid}/answers", headers=other, json={"body": "Hidden"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            board(c) + f"/{qid}/answers", headers=c["member"], json={"body": "Blocked"}
        ).status_code
        == 403
    )
    settings(c, board_comment_scope_policy="choice")
    assert (
        client.post(
            board(c) + f"/{qid}/answers",
            headers=c["member"],
            json={"body": "Allowed again"},
        ).status_code
        == 200
    )


def test_forced_all_comments_respect_private_question_read_access(context):
    c = context
    qid = post(c, visibility="private", comment_scope="operators").json()["data"][
        "contest_question_id"
    ]
    settings(c, board_comment_scope_policy="all")
    assert (
        client.post(
            board(c) + f"/{qid}/answers",
            headers=c["member"],
            json={"body": "Follow-up"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            board(c) + f"/{qid}/answers",
            headers=other_participant(c),
            json={"body": "Hidden"},
        ).status_code
        == 404
    )


def test_staff_can_change_a_questions_scope_but_not_bypass_forced_contest_policy(
    context,
):
    c = context
    qid = post(c).json()["data"]["contest_question_id"]
    endpoint = f"/api/operator/contests/{c['cid']}/boards/{qid}"
    assert (
        client.patch(
            endpoint, headers=c["member"], json={"comment_scope": "operators"}
        ).status_code
        == 401
    )
    result = client.patch(
        endpoint, headers=c["owner"], json={"comment_scope": "operators"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["effective_comment_scope"] == "operators"
    settings(c, board_comment_scope_policy="operators")
    assert (
        client.patch(
            endpoint, headers=c["owner"], json={"comment_scope": "all"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("board_comment_scope_policy", "public"),
        ("board_question_visibility_policy", "all"),
        ("board_comment_default_scope", "private"),
        ("board_question_default_visibility", "operators"),
    ],
)
def test_invalid_policy_values_and_participant_settings_changes_are_rejected(
    context, field, value
):
    c = context
    endpoint = f"/api/operator/contests/{c['cid']}/settings"
    assert (
        client.patch(endpoint, headers=c["owner"], json={field: value}).status_code
        == 422
    )
    assert (
        client.patch(
            endpoint,
            headers=c["member"],
            json={"board_comment_scope_policy": "operators"},
        ).status_code
        == 401
    )


def test_participant_preview_enforces_writing_policy_without_real_posts(preview):
    c = preview
    select_division(c)
    store.update_contest_settings(
        c["cid"],
        board_question_visibility_policy="private",
        board_comment_scope_policy="operators",
    )
    auth = headers(c["sessions"][0])
    endpoint = c["base"] + "/boards"
    assert (
        client.post(
            endpoint,
            headers=auth,
            json={"title": "Preview", "body": "Body", "visibility": "public"},
        ).status_code
        == 403
    )
    response = client.post(
        endpoint, headers=auth, json={"title": "Preview", "body": "Body"}
    )
    assert response.status_code == 200, response.text
    question = response.json()["data"]
    assert (
        question["visibility"] == "private" and question["comment_scope"] == "operators"
    )
    assert (
        client.post(
            endpoint + f"/{question['contest_question_id']}/answers",
            headers=auth,
            json={"body": "Blocked"},
        ).status_code
        == 403
    )
    assert store.questions_for_view(c["cid"], operator=True) == []


def test_board_policy_migration_preserves_existing_data_and_is_idempotent():
    path = (
        Path(__file__).parents[1] / "migrations/versions/0055_board_writing_policy.py"
    )
    spec = importlib.util.spec_from_file_location("board_policy_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE contests (contest_id VARCHAR(36) PRIMARY KEY)")
        )
        connection.execute(
            text(
                "CREATE TABLE contest_questions (question_id VARCHAR(36) PRIMARY KEY, visibility VARCHAR(16))"
            )
        )
        connection.execute(text("INSERT INTO contests VALUES ('retained')"))
        connection.execute(
            text("INSERT INTO contest_questions VALUES ('retained','private')")
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            assert connection.execute(
                text(
                    "SELECT board_question_visibility_policy, board_question_default_visibility, board_comment_scope_policy, board_comment_default_scope FROM contests"
                )
            ).one() == ("choice", "public", "choice", "all")
            assert connection.execute(
                text("SELECT visibility, comment_scope FROM contest_questions")
            ).one() == ("private", "all")
            migration.downgrade()
            assert (
                connection.execute(
                    text("SELECT visibility FROM contest_questions")
                ).scalar_one()
                == "private"
            )
            migration.upgrade()
    engine.dispose()


def test_reply_waiting_on_policy_change_uses_committed_restriction(context):
    if SessionLocal.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locking")
    c = context
    qid = post(c).json()["data"]["contest_question_id"]
    participant = store.get_participant_by_access_token(
        c["cid"], c["member"]["Authorization"].split(" ", 1)[1]
    )
    started = threading.Event()

    def reply():
        started.set()
        try:
            store.create_answer(
                c["cid"],
                qid,
                "Pending reply",
                "public",
                c["email"],
                participant=participant,
            )
            return 200
        except AppError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SessionLocal() as db:
            row = db.scalar(
                select(ContestRow)
                .where(ContestRow.contest_id == c["cid"])
                .with_for_update()
            )
            row.board_comment_scope_policy = "operators"
            task = pool.submit(reply)
            assert started.wait(5)
            db.commit()
        assert task.result(timeout=10) == 403
    assert store.get_contest_question(c["cid"], qid).answers == []
