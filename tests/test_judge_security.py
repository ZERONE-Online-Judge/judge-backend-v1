from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import app
from app.models import now_utc
from app.orm_models import JudgeJobRow, JudgeNodeRow, SubmissionRow
from app.services.store import store
from app.settings import settings
from app.tools import judge_nodes


client = TestClient(app)
SECRET = "approved-agent-secret-with-32-chars"


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'security.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(store, "_session", sessions)
    monkeypatch.setattr(judge_nodes, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.fixture
def leased(database):
    node = store.provision_node("approved-node", SECRET, 2)
    with database() as db:
        db.add(SubmissionRow(submission_id="submission", contest_id="contest", division_id="division",
            problem_id="problem", language="python311", source_code="print(42)", status="judging"))
        db.add(JudgeJobRow(judge_job_id="job", submission_id="submission", contest_id="contest",
            division_id="division", status="running", queue_position=1,
            assigned_node_id=node.judge_node_id, lease_token="original-lease", leased_at=now_utc()))
        db.commit()
    store.register_node(node.node_name, SECRET, 2, "0.3.0", ["judge-zoj-runtime-v1"], "runtime-v1")
    return node


def send(operation, **extra):
    payload = {"node_secret": SECRET, "lease_token": "original-lease"}
    if operation == "result":
        payload["final_status"] = "accepted"
    elif operation == "progress":
        payload["status"] = "judging"
    payload.update(extra)
    return client.post(f"/api/internal/judge/jobs/job/{operation}", json=payload)


def test_unknown_node_cannot_self_enroll_or_choose_its_own_secret(database):
    response = client.post("/api/internal/judge/nodes/register", json={
        "node_name": "intruder", "node_secret": SECRET, "total_slots": 100,
    })
    assert response.status_code == 403
    with database() as db:
        assert db.scalar(select(JudgeNodeRow)) is None


def test_approved_node_reconnects_and_old_credentials_are_not_pruned(database):
    node = store.provision_node("approved-node", SECRET, 2)
    assert client.get("/api/public/judge-status").json()["data"]["active_node_count"] == 0
    with database() as db:
        db.get(JudgeNodeRow, node.judge_node_id).last_heartbeat_at = now_utc() - timedelta(days=7)
        db.commit()
    assert node.judge_node_id in store.judge_nodes
    denied = client.post("/api/internal/judge/nodes/register", json={
        "node_name": node.node_name, "node_secret": "replacement", "total_slots": 2,
    })
    assert denied.status_code == 403
    response = client.post("/api/internal/judge/nodes/register", json={
        "node_name": node.node_name, "node_secret": SECRET, "total_slots": 2,
    })
    assert response.status_code == 200
    assert response.json()["data"]["judge_node_id"] == node.judge_node_id


@pytest.mark.parametrize("operation", ["result", "progress", "lease:renew"])
def test_wrong_owner_or_lease_cannot_modify_job(database, leased, operation):
    other = store.provision_node("other-node", "another-secret", 1)
    assert other.judge_node_id != leased.judge_node_id
    assert send(operation, node_secret="another-secret").status_code == 403
    assert send(operation, lease_token="forged-token").status_code == 409
    assert send(operation, lease_token="위조된-token").status_code == 409
    with database() as db:
        assert db.get(SubmissionRow, "submission").status == "judging"
        assert db.get(JudgeJobRow, "job").lease_token == "original-lease"


@pytest.mark.parametrize("operation", ["result", "progress", "lease:renew"])
@pytest.mark.parametrize("state", ["expired", "missing_timestamp", "pending", "succeeded"])
def test_expired_or_inactive_lease_cannot_report_or_revive(database, leased, operation, state):
    with database() as db:
        job = db.get(JudgeJobRow, "job")
        if state == "expired":
            job.leased_at = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds + 1)
        elif state == "missing_timestamp":
            job.leased_at = None
        else:
            job.status = state
        db.commit()
    response = send(operation)
    assert response.status_code == 409
    with database() as db:
        assert db.get(SubmissionRow, "submission").status == "judging"


def test_completed_result_cannot_be_overwritten_or_reopened(database, leased):
    assert send("result", final_status="wrong_answer").status_code == 200
    assert send("result", final_status="accepted").status_code == 409
    assert send("progress").status_code == 409
    assert send("lease:renew").status_code == 409
    with database() as db:
        assert db.get(SubmissionRow, "submission").status == "wrong_answer"
        assert db.get(JudgeJobRow, "job").lease_token is None


def test_live_lease_can_report_progress_renew_and_finish(database, leased):
    assert send("progress", status="preparing").status_code == 200
    assert send("lease:renew").status_code == 200
    assert send("progress", status="judging", progress_current=1, progress_total=1).status_code == 200
    assert send("result").status_code == 200


@pytest.mark.parametrize("status", ["accepted", "wrong_answer", "system_error", "waiting"])
def test_progress_cannot_set_a_final_verdict(database, leased, status):
    assert send("progress", status=status).status_code == 422
    with database() as db:
        assert db.get(SubmissionRow, "submission").status == "judging"


@pytest.mark.parametrize("status", ["waiting", "preparing", "judging"])
def test_result_requires_a_terminal_verdict(database, leased, status):
    assert send("result", final_status=status).status_code == 422


def test_revocation_blocks_all_agent_operations_and_requeues_work(database, leased):
    judge_nodes.update_credential(leased.node_name, enabled=False)
    base = f"/api/internal/judge/nodes/{leased.judge_node_id}"
    for endpoint, payload in [
        ("/api/internal/judge/nodes/register", {"node_name": leased.node_name, "total_slots": 2}),
        (base + "/heartbeat", {"total_slots": 2, "free_slots": 2, "running_job_count": 0}),
        (base + "/assignments:claim", {"max_count": 1}),
        (base + "/logs", {"logs": []}),
    ]:
        assert client.post(endpoint, json={"node_secret": SECRET, **payload}).status_code == 403
    for operation in ["result", "progress", "lease:renew"]:
        assert send(operation).status_code == 403
    with database() as db:
        assert db.get(JudgeJobRow, "job").status == "pending"
        assert db.get(JudgeJobRow, "job").lease_token is None
        assert db.get(SubmissionRow, "submission").status == "waiting"


def test_rotation_invalidates_old_secret_and_outstanding_lease(database, leased):
    new_secret = "replacement-agent-secret-with-32-chars"
    judge_nodes.update_credential(leased.node_name, secret=new_secret)
    assert not store.verify_node_secret(leased.judge_node_id, SECRET)
    assert store.verify_node_secret(leased.judge_node_id, new_secret)
    assert send("result", node_secret=new_secret).status_code == 403


def test_duplicate_provision_does_not_replace_an_existing_credential(database, leased):
    with pytest.raises(ValueError, match="already provisioned"):
        store.provision_node(leased.node_name, "replacement", 1)
    assert store.verify_node_secret(leased.judge_node_id, SECRET)
