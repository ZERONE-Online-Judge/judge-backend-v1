"""Retry progress is descriptive, authenticated, and never a second testcase."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import Submission, SubmissionStatus, now_utc
from app.orm_models import EditorJudgeRunRow, JudgeJobRow, SubmissionRow
from app.routers.participant import _participant_submission_payload
from app.services import editor_judging, rejudging
from app.services.store import store
from app import scoring
from app.judge_timing import TIMING_FIELDS
from tests.test_judge_security import database, leased, send, SECRET


def test_retry_phase_changes_description_without_increasing_completed_tests(database, leased):
    response = send("progress", progress_current=4, progress_total=15, progress_phase="tle_confirmation")
    assert response.status_code == 200, response.text
    saved = store.submissions["submission"]
    assert (saved.status, saved.progress_current, saved.progress_total, saved.progress_phase) == ("judging", 4, 15, "tle_confirmation")
    assert send("progress", progress_current=4, progress_total=15).status_code == 200
    saved = store.submissions["submission"]
    assert (saved.progress_current, saved.progress_phase) == (4, None)
    assert send("progress", progress_current=4, progress_total=15, progress_phase="tle_confirmation").status_code == 200
    assert send("result", final_status="time_limit_exceeded", failed_testcase_order=5).status_code == 200
    assert store.submissions["submission"].progress_phase is None


@pytest.mark.parametrize("extra", [{"status": "preparing", "progress_phase": "tle_confirmation"}, {"progress_phase": "arbitrary"}])
def test_retry_phase_cannot_be_used_as_a_verdict_or_free_text(database, leased, extra):
    assert send("progress", **extra).status_code == 422
    assert store.submissions["submission"].progress_phase is None


def test_participant_progress_policy_also_controls_retry_description():
    submission = Submission(contest_id="c", division_id="d", problem_id="p", language="cpp17", source_code="",
        status="judging", progress_current=4, progress_total=15, progress_phase="tle_confirmation")
    visible = _participant_submission_payload(submission, include_source=False, show_progress=True)
    hidden = _participant_submission_payload(submission, include_source=False, show_progress=False)
    assert visible["progress_phase"] == "tle_confirmation" and visible["progress_percent"] == 27
    assert hidden["progress_phase"] is None and hidden["progress_percent"] is None
    assert visible["judge_message"] is None and visible["progress_current"] is None


def test_expired_lease_requeues_without_stale_retry_state(database, leased):
    with database() as db:
        db.get(SubmissionRow, "submission").progress_phase = "tle_confirmation"
        db.get(JudgeJobRow, "job").leased_at = now_utc() - timedelta(days=1)
        db.commit()
        store._recover_expired_judge_leases(db)
        db.commit()
        row = db.get(SubmissionRow, "submission")
        assert row.status == "waiting" and row.progress_phase is None


def test_rejudging_candidate_phase_is_independent_and_clears_after_result():
    item = SimpleNamespace(candidate_result={"status": "judging", "progress_current": 3, "progress_total": 8})
    rejudging.record_progress(item, SubmissionStatus.JUDGING, 3, 8, progress_phase="tle_confirmation")
    assert item.candidate_result["progress_phase"] == "tle_confirmation"
    assert item.candidate_result["progress_current"] == 3
    rejudging.record_result(item, SubmissionStatus.TIME_LIMIT_EXCEEDED, None, None, 4, 1, 1)
    assert item.candidate_result["progress_phase"] is None


def test_editor_run_phase_uses_existing_counts_and_clears_on_final_result(database, leased):
    with database() as db:
        db.add(EditorJudgeRunRow(run_id="editor", operation_key="operation", owner_id="owner", project_id="project",
            fingerprint="f", payload={"runtime_revision": "runtime", "bundle_key": "same", "base_bundle_key": "same", "testcases": [{"display_order": 1}, {"display_order": 2}]},
            storage_keys=[], status="judging", testcase_count=2, progress_current=1, assigned_node_id=leased.judge_node_id,
            lease_token="editor-lease", leased_at=now_utc()))
        db.commit()
    report = editor_judging.update("editor", SECRET, "editor-lease", status="judging", current=1, total=2,
        progress_phase="tle_confirmation", session_factory=database)["editor_run"]
    assert (report["progress_current"], report["progress_total"], report["progress_phase"]) == (1, 2, "tle_confirmation")
    result = {"final_status": "time_limit_exceeded", "failed_testcase_order": 2}
    report = editor_judging.update("editor", SECRET, "editor-lease", result=result, session_factory=database)["editor_run"]
    assert report["progress_phase"] is None


def evidence(**extra):
    return {"index": 1, "final_status": "accepted", **extra}


def test_timing_metadata_survives_ioi_and_editor_evidence_without_affecting_verdict():
    incoming = evidence(cpu_runtime_ms=10, wall_runtime_ms=21, cpu_limit_ms=1000, wall_limit_ms=3000,
        timeout_origin=None, retry_reason="wall", attempts=2)
    value = editor_judging.case_result_value(incoming)
    assert value["attempts"] == 2 and value["retry_reason"] == "wall"
    assert "timeout_origin" not in value
    merged = scoring.merge_evidence({"testcase_orders": [1]}, [], [incoming])
    assert merged[0]["cpu_runtime_ms"] == 10 and merged[0]["retry_reason"] == "wall"
    assert scoring.merge_evidence({"testcase_orders": [1]}, merged, [incoming]) == merged
    assert scoring.merge_evidence({"testcase_orders": [1]}, [evidence()], [evidence()]) == [evidence()]
    assert all(key not in editor_judging.case_result_value(evidence()) for key in TIMING_FIELDS)


@pytest.mark.parametrize("extra", [
    {"cpu_runtime_ms": -1}, {"wall_runtime_ms": float("inf")}, {"cpu_limit_ms": 1.5}, {"wall_limit_ms": "100"},
    {"attempts": 0}, {"attempts": 3}, {"attempts": True}, {"timeout_origin": "other"}, {"retry_reason": "other"},
])
def test_timing_evidence_rejects_unbounded_or_ambiguous_measurements(extra):
    with pytest.raises(ValidationError):
        editor_judging.CaseResult.model_validate(evidence(**extra))


def test_editor_full_suite_persists_retry_measurements_and_same_result_is_idempotent(database, leased):
    with database() as db:
        db.add(EditorJudgeRunRow(run_id="measured", operation_key="measured-operation", owner_id="owner", project_id="project",
            fingerprint="f", payload={"runtime_revision": "runtime", "bundle_key": "same", "base_bundle_key": "same",
                "collect_all_results": True, "testcases": [{"display_order": 1}]},
            storage_keys=[], status="judging", testcase_count=1, progress_current=0, assigned_node_id=leased.judge_node_id,
            lease_token="measured-lease", leased_at=now_utc()))
        db.commit()
    measured = evidence(cpu_runtime_ms=12, wall_runtime_ms=23, cpu_limit_ms=1000, wall_limit_ms=3000,
        retry_reason="wall", attempts=2)
    result = {"final_status": "accepted", "failed_testcase_order": None, "testcase_results": [measured]}
    first = editor_judging.update("measured", SECRET, "measured-lease", result=result, session_factory=database)
    second = editor_judging.update("measured", SECRET, "measured-lease", result=result, session_factory=database)
    assert first == second
    with database() as db:
        saved = db.get(EditorJudgeRunRow, "measured").result["testcase_results"][0]
        assert saved["retry_reason"] == "wall" and saved["attempts"] == 2
        assert saved["cpu_runtime_ms"] == 12 and saved["wall_runtime_ms"] == 23
