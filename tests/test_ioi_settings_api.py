"""Configuration endpoints reject unsupported/invalid or stale score settings."""
from copy import deepcopy
import hashlib

from app.models import now_utc
from app.orm_models import SubmissionRow
from app.services.store import store
from app.services.storage import object_storage
from tests.test_presentation_alert_settings import context
from tests.test_ioi_subtasks import GROUPS


def test_settings_and_subtask_editor_share_strict_persisted_contract(context):
    c = context
    response = c["client"].patch(c["path"], headers=c["headers"], json={"scoring_mode": "ioi"})
    assert response.status_code == 200 and response.json()["data"]["scoring_mode"] == "ioi"
    assert response.json()["data"]["scoring_mode_locked"] is False
    division = store.contest_divisions(c["cid"])[0]
    problem = store.create_problem(c["cid"], division.division_id, "A", "Subtasks", "", 1000, 256, {}, 1)
    active = store.create_testcase_set(c["cid"], problem.problem_id, True)
    for index in (1, 2):
        key = f"contests/{c['cid']}/problems/{problem.problem_id}/tests/{index}"
        content = f"{index}\n".encode()
        object_storage.write_bytes(key, content)
        sha = hashlib.sha256(content).hexdigest()
        store.add_testcase(c["cid"], problem.problem_id, active.testcase_set_id, index, key, key, sha, sha, None, None)
    path = f"/api/operator/contests/{c['cid']}/problems/{problem.problem_id}/testcase-sets/{active.testcase_set_id}"
    response = c["client"].patch(path, headers=c["headers"], json={"subtasks": GROUPS, "expected_subtasks": []})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["subtasks"] == GROUPS
    assert response.json()["data"]["subtasks_locked"] is False
    stale = c["client"].patch(path, headers=c["headers"], json={"subtasks": [], "expected_subtasks": []})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "subtasks_conflict"
    invalid = deepcopy(GROUPS)
    invalid[0]["points"] = 30.0
    rejected = c["client"].patch(path, headers=c["headers"], json={"subtasks": invalid})
    assert rejected.status_code == 422
    with store._session() as db:
        db.add(SubmissionRow(contest_id=c["cid"], division_id=division.division_id, problem_id=problem.problem_id,
            language="cpp17", source_code="int main(){}", status="waiting", submission_kind="participant", scoring_mode="ioi"))
        db.commit()
    locked = c["client"].patch(c["path"], headers=c["headers"], json={"scoring_mode": "icpc"})
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "scoring_mode_locked"
    rejected = c["client"].patch(path, headers=c["headers"], json={"subtasks": []})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "subtasks_locked"


def test_invalid_mode_is_rejected_before_unrelated_settings_change(context):
    c = context
    response = c["client"].patch(c["path"], headers=c["headers"], json={"scoring_mode": "points", "title": "Do not save"})
    assert response.status_code == 422
    assert store.get_contest(c["cid"]).title != "Do not save"
