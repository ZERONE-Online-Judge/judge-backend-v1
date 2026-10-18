import pytest
from sqlalchemy import select

from app.orm_models import JudgeJobRow, ProblemRow, RejudgingItemRow, RejudgingRow, SubmissionRow
from app.routers import operator
from app.services import rejudging
from app.services.store import store
from tests.test_judge_security import client, database, leased
from tests.test_staged_rejudging import ACTOR, claim, create, finished, report


@pytest.fixture
def delete_client(database, finished, monkeypatch):
    actor = ACTOR.model_copy(update={"contest_scopes": {"contest": [
        "contest.problem.manage", "contest.submission.view", "contest.problem.test",
    ]}})
    monkeypatch.setattr(operator, "require_contest_staff", lambda *args: actor)
    monkeypatch.setattr(operator, "_require_contest_mutation_open", lambda cid: None)
    return client, actor


@pytest.mark.parametrize("state", ["queued", "running", "ready", "blocked"])
def test_confirmed_deletion_cancels_rejudging_before_removing_problem(delete_client, database, finished, state):
    api, _ = delete_client
    batch = create()
    job = claim(finished)[0] if state != "queued" else None
    if state in {"ready", "blocked"}:
        from app.models import SubmissionStatus
        report(job, SubmissionStatus.SYSTEM_ERROR if state == "blocked" else SubmissionStatus.ACCEPTED)
    response = api.delete("/api/operator/contests/contest/problems/problem?cancel_rejudging=true")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["problem_id"] == "problem"
    with database() as db:
        assert db.get(ProblemRow, "problem") is None
        assert db.get(SubmissionRow, "submission") is None
        assert db.get(RejudgingRow, batch["rejudging_id"]) is None
        assert db.scalar(select(RejudgingItemRow)) is None
        assert db.scalar(select(JudgeJobRow)) is None
    if state == "running":
        assert report(job) is None
        with database() as db:
            assert db.get(SubmissionRow, "submission") is None


def test_unconfirmed_deletion_preserves_active_rejudging(delete_client, database):
    api, _ = delete_client
    batch = create()
    response = api.delete("/api/operator/contests/contest/problems/problem")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "rejudging_conflict"
    with database() as db:
        assert db.get(ProblemRow, "problem") is not None
        assert db.get(RejudgingRow, batch["rejudging_id"]).status == "pending"


@pytest.mark.parametrize("permissions", [[], ["contest.submission.view"], ["contest.problem.test"]])
def test_problem_manager_needs_rejudging_permission_to_cancel_pending_work(delete_client, database, permissions):
    api, actor = delete_client
    actor.contest_scopes = {"contest": ["contest.problem.manage", *permissions]}
    batch = create()
    response = api.delete("/api/operator/contests/contest/problems/problem?cancel_rejudging=true")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "problem_deletion_rejudging_permission"
    with database() as db:
        assert db.get(ProblemRow, "problem") is not None
        assert db.get(RejudgingRow, batch["rejudging_id"]).status == "pending"


def test_problem_manager_can_delete_without_rejudging_permission_if_no_work_is_pending(delete_client):
    api, actor = delete_client
    actor.contest_scopes = {"contest": ["contest.problem.manage"]}
    response = api.delete("/api/operator/contests/contest/problems/problem?cancel_rejudging=true")
    assert response.status_code == 200, response.text


def test_pending_lookup_is_scoped_to_contest_and_problem(delete_client):
    create()
    assert len(rejudging.pending_for_problem(store, "contest", "problem")) == 1
    assert rejudging.pending_for_problem(store, "other", "problem") == []
    assert rejudging.pending_for_problem(store, "contest", "other") == []


def test_conflicting_cancellation_does_not_delete_problem(delete_client, database, monkeypatch):
    from app.services.errors import AppError
    api, _ = delete_client
    batch = create()
    def conflict(*args):
        raise AppError(409, "rejudging_conflict", "다른 운영자가 이미 처리한 작업입니다.")
    monkeypatch.setattr(rejudging, "decide", conflict)
    response = api.delete("/api/operator/contests/contest/problems/problem?cancel_rejudging=true")
    assert response.status_code == 409
    with database() as db:
        assert db.get(ProblemRow, "problem") is not None
        assert db.get(RejudgingRow, batch["rejudging_id"]) is not None
