"""Official IOI fixed-score accumulation, immutable evidence and freeze boundaries."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import os
from uuid import uuid4
from io import BytesIO
from types import SimpleNamespace
from threading import Event, get_ident

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.orm import sessionmaker

from app import scoring
from app.database import Base
from app.models import ContestStatus, StaffAccount, SubmissionStatus, now_utc
from app.orm_models import ContestRow, JudgeJobRow, ParticipantTeamRow, SubmissionRow, TeamMemberRow, TestcaseRow as CaseRow, TestcaseSetRow as CaseSetRow
from app.services import problem_archive, rejudging
from app.services.errors import AppError, TestcaseResultsInvalid as EvidenceInvalid
from app.services.storage import object_storage
from app.services.store import store
from app.settings import settings

SECRET = "subtask-test-agent-secret-at-least-32-characters"
GROUPS = [{"subtask_id": "small", "name": "작은 입력", "points": 30, "testcase_orders": [1]},
          {"subtask_id": "large", "name": "전체 조건", "points": 70, "testcase_orders": [2]}]
ACTOR = StaffAccount(staff_account_id="operator", email="operator@example.com", display_name="Operator")


@pytest.fixture
def ioi_database(tmp_path):
    url = os.environ.get("IOI_TEST_DATABASE_URL")
    if not url:
        engine = create_engine(f"sqlite:///{tmp_path / 'subtasks.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        try:
            yield engine
        finally:
            engine.dispose()
        return
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or parsed.database != "editor_judge_test":
        pytest.fail("IOI_TEST_DATABASE_URL must use the disposable editor_judge_test PostgreSQL database")
    # Never use public tables: every test owns a cryptographically distinct
    # schema. Commit/rollback tests exercise actual PostgreSQL transactions.
    schema = "ioi_subtasks_test_" + uuid4().hex
    control = create_engine(url)
    engine = create_engine(url)
    created = False
    try:
        with control.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        quoted = engine.dialect.identifier_preparer.quote_schema(schema)

        @event.listens_for(engine, "connect")
        def isolated_search_path(dbapi_connection, _record):
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f"SET search_path TO {quoted}, pg_catalog")
            dbapi_connection.commit()

        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        try:
            if created:
                with control.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            control.dispose()


@pytest.fixture
def context(ioi_database, tmp_path, monkeypatch):
    sessions = sessionmaker(bind=ioi_database, expire_on_commit=False)
    monkeypatch.setattr(store, "_session", sessions)
    monkeypatch.setattr(problem_archive, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "local_object_storage_root", str(tmp_path / "objects"))
    now = now_utc()
    contest = store.create_contest("IOI", "ZOJ", "", now - timedelta(hours=3), now - timedelta(hours=1), now - timedelta(hours=2),
        status=ContestStatus.ENDED, scoring_mode="ioi")
    division = store.create_contest_division(contest.contest_id, "all", "전체")
    problem = store.create_problem(contest.contest_id, division.division_id, "A", "부분 점수", "", 1000, 256, {}, 1)
    active = store.create_testcase_set(contest.contest_id, problem.problem_id, True)
    for order in (1, 2):
        content = f"{order}\n".encode()
        key = f"contests/{contest.contest_id}/problems/{problem.problem_id}/tests/{order}"
        object_storage.write_bytes(key, content)
        sha = hashlib.sha256(content).hexdigest()
        store.add_testcase(contest.contest_id, problem.problem_id, active.testcase_set_id, order, key, key, sha, sha, None, None)
    store.update_testcase_set(contest.contest_id, problem.problem_id, active.testcase_set_id, subtasks=deepcopy(GROUPS))
    with sessions() as db:
        for index in (1, 2):
            team = ParticipantTeamRow(participant_team_id=f"team{index}", contest_id=contest.contest_id,
                division_id=division.division_id, team_name=f"팀 {index}")
            db.add(team)
            db.flush()
            db.add(TeamMemberRow(team_member_id=f"member{index}", contest_id=contest.contest_id,
                participant_team_id=team.participant_team_id, role="leader", name=f"참가자{index}", email=f"team{index}@example.com"))
        db.commit()
    node = store.provision_node("subtask-agent", SECRET, 8)
    store.register_node(node.node_name, SECRET, 8, "test", [scoring.CAPABILITY, "judge-zoj-runtime-v1"], "test")
    yield SimpleNamespace(cid=contest.contest_id, did=division.division_id, pid=problem.problem_id,
        setid=active.testcase_set_id, sessions=sessions, node=node)


def create(c, team=1, claim=True, before_freeze=False):
    sub = store.create_submission(c.cid, c.pid, f"team{team}@example.com", "cpp17", "int main(){}")
    with c.sessions() as db:
        db.get(SubmissionRow, sub.submission_id).submitted_at = now_utc() - timedelta(hours=2, minutes=1) if before_freeze else now_utc() - timedelta(hours=1, minutes=1)
        db.commit()
    jobs = store.claim_jobs(c.node.judge_node_id, SECRET, 8) if claim else []
    return sub.submission_id, next((job for job in jobs if job["submission_id"] == sub.submission_id), None)


def finish(job, statuses=("accepted", "accepted"), terminal=None, results=None):
    failures = [(i, status) for i, status in enumerate(statuses, 1) if status != "accepted"]
    order, actual = failures[0] if failures else (None, "accepted")
    status = terminal or actual
    if terminal in {"compile_error", "system_error"}:
        order = None
    cases = results if results is not None else [{"index": i, "final_status": value} for i, value in enumerate(statuses, 1)]
    return store.report_judge_result(job["judge_job_id"], SECRET, job["lease_token"], SubmissionStatus(status), None, None,
        order, testcase_results=cases)[0]


def board(c, public=False):
    return store.scoreboard_rows(c.cid, c.did, public_view=public)


def test_independent_subtasks_accumulate_across_complementary_wa_submissions(context):
    c = context
    sid1, job1 = create(c)
    assert job1["scoring_mode"] == "ioi" and job1["collect_all_results"] is True
    assert job1["testcase_set"]["subtasks"] == GROUPS
    first = finish(job1, ("accepted", "wrong_answer"))
    assert first.status == "wrong_answer" and first.score == 30
    _, job2 = create(c)
    second = finish(job2, ("wrong_answer", "accepted"))
    assert second.score == 70
    own = store.participant_ioi_scores(c.cid, "team1")[c.pid]
    assert own["score"] == 100 and own["max_score"] == 100
    assert "testcase_orders" not in str(own)
    result = board(c)
    row = result["rows"][0]
    assert result["scoring_mode"] == "ioi"
    assert row["total_score"] == 100 and row["solved"] == 1 and row["penalty"] is None
    assert row["problem_scores"][0]["score"] == 100
    stats = result["problem_stats"][0]
    assert stats["accepted_submissions"] == 0 and stats["accepted_team_count"] == 1
    assert stats["team_acceptance_rate"] == 100 and stats["total_team_count"] == 2


def test_equal_score_is_shared_rank_without_time_or_penalty_tie(context):
    c = context
    _, early = create(c, 1, before_freeze=True)
    finish(early, ("accepted", "wrong_answer"))
    _, later = create(c, 2)
    finish(later, ("accepted", "time_limit_exceeded"))
    rows = board(c)["rows"]
    assert [row["total_score"] for row in rows] == [30, 30]
    assert [row["rank"] for row in rows] == [1, 1]


@pytest.mark.parametrize("status,score", [("compile_error", 0), ("system_error", None)])
def test_compile_error_zero_and_system_error_no_official_gain(context, status, score):
    c = context
    _, job = create(c)
    result = finish(job, terminal=status, results=[])
    assert result.score == score
    assert len(result.subtask_results) == (2 if status == "compile_error" else 0)
    assert board(c)["rows"][0]["total_score"] == 0


def test_full_score_and_result_retries_are_idempotent(context):
    _, job = create(context)
    assert finish(job).score == 100
    assert finish(job).score == 100
    with pytest.raises(ValueError, match="lease"):
        finish(job, ("wrong_answer", "accepted"))


@pytest.mark.parametrize("evidence", [[], [{"index": 1, "final_status": "accepted"}],
    [{"index": 1, "final_status": "accepted"}, {"index": 1, "final_status": "accepted"}],
    [{"index": 1, "final_status": "accepted"}, {"index": 3, "final_status": "accepted"}]])
def test_missing_duplicate_or_forged_evidence_is_rejected_atomically(context, evidence):
    c = context
    sid, job = create(c)
    with pytest.raises(EvidenceInvalid):
        finish(job, results=evidence)
    assert store.get_submission(sid).score is None
    with c.sessions() as db:
        assert db.get(JudgeJobRow, job["judge_job_id"]).lease_token == job["lease_token"]
    assert finish(job).score == 100


def test_progress_evidence_cannot_change_or_forge_prior_observation(context):
    _, job = create(context)
    store.update_judge_progress(job["judge_job_id"], SECRET, job["lease_token"], SubmissionStatus.JUDGING, 1, 2,
        testcase_result={"index": 1, "final_status": "accepted"})
    with pytest.raises(EvidenceInvalid):
        finish(job, ("wrong_answer", "accepted"))
    assert finish(job, ("accepted", "wrong_answer")).score == 30


def test_old_agents_never_receive_ioi_work(context):
    c = context
    sid, _ = create(c, claim=False)
    store.register_node(c.node.node_name, SECRET, 8, "legacy", ["judge-zoj-runtime-v1"], "old")
    assert store.claim_jobs(c.node.judge_node_id, SECRET, 8) == []
    assert store.get_submission(sid).status == "waiting"
    store.register_node(c.node.node_name, SECRET, 8, "new", [scoring.CAPABILITY, "judge-zoj-runtime-v1"], "new")
    assert len(store.claim_jobs(c.node.judge_node_id, SECRET, 8)) == 1


def test_frozen_scores_no_hidden_groups_and_resolver_reveal_undo(context):
    c = context
    _, pre = create(c, before_freeze=True)
    finish(pre, ("accepted", "wrong_answer"))
    _, post = create(c)
    finish(post, ("wrong_answer", "accepted"))
    frozen = board(c, True)
    assert frozen["rows"][0]["total_score"] == 30
    cell = frozen["rows"][0]["problem_scores"][0]
    assert "subtask_scores" not in cell and cell["best_submission_id"] is None
    store.update_scoreboard_release(c.cid, c.did, "start")
    after = store.update_scoreboard_release(c.cid, c.did, "next")
    assert after["resolver"]["last_event"]["score_delta"] == 70
    assert board(c, True)["rows"][0]["total_score"] == 100
    store.update_scoreboard_release(c.cid, c.did, "undo", expected_revision=after["revision"])
    assert board(c, True)["rows"][0]["total_score"] == 30
    assert store.participant_ioi_scores(c.cid, "team1")[c.pid]["score"] == 100


def test_mode_group_and_active_version_locks(context):
    c = context
    active = store.testcase_sets_for_problem(c.cid, c.pid)[0]
    with pytest.raises(AppError) as stale:
        store.update_testcase_set(c.cid, c.pid, c.setid, subtasks=GROUPS, expected_subtasks=[])
    assert stale.value.code == "subtasks_conflict"
    _, job = create(c)
    finish(job)
    assert store.get_contest(c.cid).scoring_mode_locked
    with pytest.raises(AppError) as incompatible:
        store.create_testcase_set(c.cid, c.pid, True)
    assert incompatible.value.code == "subtasks_locked"
    with pytest.raises(AppError) as locked:
        store.update_contest_settings(c.cid, scoring_mode="icpc")
    assert locked.value.code == "scoring_mode_locked"
    changed = deepcopy(GROUPS)
    changed[0]["name"] = "changed"
    with pytest.raises(AppError):
        store.update_testcase_set(c.cid, c.pid, c.setid, subtasks=changed)
    with c.sessions() as db:
        replacement = CaseSetRow(problem_id=c.pid, version=2, is_active=False, subtasks=[
            {**GROUPS[0], "testcase_orders": [2]}, {**GROUPS[1], "testcase_orders": [1]}])
        db.add(replacement)
        db.flush()
        for old in db.scalars(select(CaseRow).where(CaseRow.testcase_set_id == c.setid)):
            db.add(CaseRow(testcase_set_id=replacement.testcase_set_id, display_order=old.display_order,
                input_storage_key=old.input_storage_key, output_storage_key=old.output_storage_key,
                input_sha256=old.input_sha256, output_sha256=old.output_sha256))
        replacement_id = replacement.testcase_set_id
        db.commit()
    with pytest.raises(AppError):
        store.update_testcase_set(c.cid, c.pid, replacement_id, is_active=True)


def test_rejudge_preserves_candidate_score_until_atomic_apply_and_can_restore(context):
    c = context
    sid, job = create(c)
    finish(job)
    batch = rejudging.create(store, c.cid, submission_id=sid, reason="check", actor=ACTOR)
    claimed = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    assert claimed["scoring_mode"] == "ioi" and claimed["collect_all_results"]
    candidate = finish(claimed, ("accepted", "wrong_answer"))
    assert candidate.score == 30 and store.get_submission(sid).score == 100
    details = rejudging.detail(store, c.cid, batch["rejudging_id"])
    assert details["changed_verdicts"] == 1
    rejudging.decide(store, c.cid, batch["rejudging_id"], "apply", ACTOR)
    assert store.get_submission(sid).score == 30
    assert board(c)["rows"][0]["total_score"] == 30
    restore = rejudging.create(store, c.cid, submission_id=sid, reason="restore", actor=ACTOR)
    candidate = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    finish(candidate)
    rejudging.decide(store, c.cid, restore["rejudging_id"], "apply", ACTOR)
    assert board(c)["rows"][0]["total_score"] == 100


def test_copy_archive_roundtrip_and_icpc_import_guard(context):
    c = context
    target = store.create_contest_division(c.cid, "other", "Other")
    clone = store.copy_problem_to_division(c.cid, c.pid, target.division_id)
    assert store.testcase_sets_for_problem(c.cid, clone.problem_id)[0]["subtasks"] == GROUPS
    archive, _ = problem_archive.export_archive(c.cid, c.pid)
    data = archive.read()
    archive.close()
    restored = problem_archive.import_archive(BytesIO(data), c.cid, c.did, "B")
    assert store.testcase_sets_for_problem(c.cid, restored["problem_id"])[0]["subtasks"] == GROUPS
    with c.sessions() as db:
        db.get(ContestRow, c.cid).scoring_mode = "icpc"
        db.commit()
    with pytest.raises(AppError) as unsupported:
        problem_archive.import_archive(BytesIO(data), c.cid, c.did, "C")
    assert unsupported.value.code == "subtasks_require_ioi"


@pytest.mark.parametrize("groups", [None, [GROUPS[0]], [GROUPS[0], GROUPS[0]],
    [{**GROUPS[0], "points": True}, GROUPS[1]], [{**GROUPS[0], "points": 30.0}, GROUPS[1]],
    [{**GROUPS[0], "testcase_orders": [1, 1]}, GROUPS[1]],
    [{**GROUPS[0], "testcase_orders": [3]}, GROUPS[1]],
    [{**GROUPS[0], "testcase_orders": []}, GROUPS[1]]])
def test_subtask_configuration_rejects_bad_weights_and_membership(groups):
    with pytest.raises(AppError):
        scoring.normalize_subtasks(groups, [1, 2])


def test_empty_ioi_suite_cannot_earn_free_100_points():
    with pytest.raises(AppError):
        scoring.snapshot(SimpleNamespace(subtasks=[], testcase_set_id="empty", version=1), [])


def test_same_total_different_groups_rejudge_changes_official_accumulation(context):
    c = context
    store.update_testcase_set(c.cid, c.pid, c.setid, subtasks=[])
    content = b"3\n"
    key = f"contests/{c.cid}/problems/{c.pid}/tests/3"
    object_storage.write_bytes(key, content)
    sha = hashlib.sha256(content).hexdigest()
    store.add_testcase(c.cid, c.pid, c.setid, 3, key, key, sha, sha, None, None)
    groups = [{"subtask_id": "a", "name": "A", "points": 30, "testcase_orders": [1]},
        {"subtask_id": "b", "name": "B", "points": 30, "testcase_orders": [2]},
        {"subtask_id": "c", "name": "C", "points": 40, "testcase_orders": [3]}]
    store.update_testcase_set(c.cid, c.pid, c.setid, subtasks=groups)
    sid, first = create(c)
    finish(first, ("accepted", "wrong_answer", "wrong_answer"))
    _, second = create(c)
    finish(second, ("accepted", "wrong_answer", "wrong_answer"))
    assert board(c)["rows"][0]["total_score"] == 30
    batch = rejudging.create(store, c.cid, submission_id=sid, reason="swap group", actor=ACTOR)
    candidate = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    assert finish(candidate, ("wrong_answer", "accepted", "wrong_answer")).score == 30
    details = rejudging.detail(store, c.cid, batch["rejudging_id"])
    assert details["changed_verdicts"] == 1 and details["items"][0]["changed"]
    assert board(c)["rows"][0]["total_score"] == 30
    rejudging.decide(store, c.cid, batch["rejudging_id"], "cancel", ACTOR)
    assert board(c)["rows"][0]["total_score"] == 30
    batch = rejudging.create(store, c.cid, submission_id=sid, reason="apply swap", actor=ACTOR)
    candidate = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    finish(candidate, ("wrong_answer", "accepted", "wrong_answer"))
    rejudging.decide(store, c.cid, batch["rejudging_id"], "apply", ACTOR)
    assert store.get_submission(sid).score == 30
    assert board(c)["rows"][0]["total_score"] == 60
    restore = rejudging.create(store, c.cid, submission_id=sid, reason="restore group", actor=ACTOR)
    candidate = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    finish(candidate, ("accepted", "wrong_answer", "wrong_answer"))
    rejudging.decide(store, c.cid, restore["rejudging_id"], "apply", ACTOR)
    assert board(c)["rows"][0]["total_score"] == 30


@pytest.mark.parametrize("probe", [False, True])
def test_ioi_ai_trial_subsets_and_custom_probes_remain_non_scoring(context, probe):
    from app.services import verification_ai as ai
    from app.orm_models import ProblemRow, VerificationTrialRow
    c = context
    with c.sessions() as db:
        submission = SubmissionRow(contest_id=c.cid, division_id=c.did, problem_id=c.pid,
            submission_kind="verification_trial", language="cpp17", source_code="int main(){}", status="waiting")
        db.add(submission)
        db.flush()
        context_hash = ai.digest(ai.current_context(db, db.get(ProblemRow, c.pid)))
        db.add(VerificationTrialRow(submission_id=submission.submission_id, analysis_id="analysis",
            contest_id=c.cid, problem_id=c.pid, context_hash=context_hash, cache_key="probe" if probe else "subset",
            artifact_id="artifact", testcase_orders=None if probe else [2], testcase_count=1,
            probe={"input": "3\n", "expected_output": "3\n"} if probe else None))
        db.add(JudgeJobRow(submission_id=submission.submission_id, contest_id=c.cid, division_id=c.did,
            status="pending", queue_position=1))
        sid = submission.submission_id
        db.commit()
    claimed = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    assert claimed["scoring_mode"] == "icpc" and "collect_all_results" not in claimed
    assert len(claimed["testcases"]) == 1
    assert claimed["testcases"][0]["display_order"] == (1 if probe else 2)
    finished, _ = store.report_judge_result(claimed["judge_job_id"], SECRET, claimed["lease_token"],
        SubmissionStatus.ACCEPTED, None, None, None)
    assert finished.score is None and finished.subtask_results == []
    with c.sessions() as db:
        assert db.get(SubmissionRow, sid).scoring_snapshot == {}


def test_ioi_registered_operator_full_verification_preserves_all_evidence(context):
    from app.services import verification_execution
    from app.orm_models import VerificationExecutionRow
    c = context
    submission = store.create_operator_test_submission(c.cid, c.pid, "cpp17", "int main(){}")
    with c.sessions() as db:
        verification_execution.attach(db, db.get(SubmissionRow, submission.submission_id))
        db.commit()
    store.register_node(c.node.node_name, SECRET, 8, "new", [scoring.CAPABILITY,
        verification_execution.CAPABILITY, "judge-zoj-runtime-v1"], "new")
    claimed = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    assert claimed["scoring_mode"] == "ioi" and claimed["collect_all_results"]
    store.update_judge_progress(claimed["judge_job_id"], SECRET, claimed["lease_token"], SubmissionStatus.JUDGING, 1, 2,
        testcase_result={"index": 1, "final_status": "wrong_answer"})
    finished = finish(claimed, ("wrong_answer", "accepted"))
    assert finished.score == 70 and finished.progress_current == 2
    with c.sessions() as db:
        execution = db.get(VerificationExecutionRow, submission.submission_id)
        assert execution.full_suite_complete and len(execution.results) == 2
        assert len(db.get(SubmissionRow, submission.submission_id).scoring_evidence) == 2
    assert all(row["total_score"] == 0 for row in board(c)["rows"])


def test_ioi_ordinary_problem_is_one_all_or_nothing_hundred_point_group(context):
    c = context
    store.update_testcase_set(c.cid, c.pid, c.setid, subtasks=[])
    _, first = create(c)
    assert finish(first, ("wrong_answer", "accepted")).score == 0
    _, second = create(c)
    full = finish(second)
    assert full.score == 100
    assert full.subtask_results == [{"subtask_id": "__all__", "name": "전체 테스트", "score": 100,
        "max_score": 100, "status": "accepted"}]
    assert board(c)["rows"][0]["total_score"] == 100


def test_claim_uses_immutable_original_configuration_and_not_later_metadata(context):
    c = context
    _, job = create(c)
    with c.sessions() as db:
        active = db.get(CaseSetRow, c.setid)
        active.subtasks = [{**GROUPS[0], "points": 60}, {**GROUPS[1], "points": 40}]
        db.commit()
    assert finish(job, ("accepted", "wrong_answer")).score == 30


def test_preview_accumulation_is_owner_scoped_and_never_affects_official_scores(context, monkeypatch):
    from app.services import participant_preview
    from app.routers.participant import _problem_solve_statuses
    c = context
    monkeypatch.setattr(participant_preview, "SessionLocal", c.sessions)
    participant = {"is_preview": True, "team": SimpleNamespace(contest_id=c.cid),
        "division": SimpleNamespace(division_id=c.did), "member": SimpleNamespace(email="preview@example.com", name="Preview")}
    for statuses in [("accepted", "wrong_answer"), ("wrong_answer", "accepted")]:
        participant_preview.create_preview_submission(c.cid, c.pid, participant, "cpp17", "int main(){}")
        job = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
        finish(job, statuses)
    own = _problem_solve_statuses(c.cid, participant)[c.pid]
    assert own["score"] == 100 and own["solve_status"] == "accepted"
    other = {**participant, "member": SimpleNamespace(email="different@example.com", name="Other")}
    assert _problem_solve_statuses(c.cid, other)[c.pid]["score"] == 0
    assert all(row["total_score"] == 0 for row in board(c)["rows"])


@pytest.mark.parametrize("kind", ["participant", "operator_test", "mock", "preview"])
def test_first_submission_serializes_scoring_changes_on_postgresql(context, ioi_database, monkeypatch, kind):
    if ioi_database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency regression")
    from app.services import participant_preview
    monkeypatch.setattr(participant_preview, "SessionLocal", context.sessions)
    c = context
    parent_thread = get_ident()
    ready, proceed = Event(), Event()

    def create_first():
        if kind == "participant":
            return store.create_submission(c.cid, c.pid, "team1@example.com", "cpp17", "int main(){}")
        if kind == "operator_test":
            return store.create_operator_test_submission(c.cid, c.pid, "cpp17", "int main(){}")
        if kind == "mock":
            return store.create_mock_submission(c.cid, c.pid, "cpp17", "int main(){}")
        participant = {"division": SimpleNamespace(division_id=c.did),
            "member": SimpleNamespace(email="preview@example.com", name="Preview")}
        return participant_preview.create_preview_submission(c.cid, c.pid, participant, "cpp17", "int main(){}")

    def before_query(_connection, cursor, statement, _parameters, _context, _many):
        if get_ident() == parent_thread and "FOR UPDATE" in statement:
            # The creator holds its transaction until the event is released.
            # A server-side timeout proves blocking without scheduler sleeps.
            cursor.execute("SET LOCAL lock_timeout = '200ms'")

    def after_query(_connection, _cursor, statement, _parameters, _context, _many):
        if get_ident() != parent_thread and "FROM problems" in statement and not ready.is_set():
            ready.set()
            assert proceed.wait(10), "first-submission transaction was not released"

    event.listen(ioi_database, "before_cursor_execute", before_query)
    event.listen(ioi_database, "after_cursor_execute", after_query)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(create_first)
            try:
                assert ready.wait(10), "first submission did not reach its context read"
                for update_settings in (
                    lambda: store.update_contest_settings(c.cid, scoring_mode="icpc"),
                    lambda: store.update_testcase_set(c.cid, c.pid, c.setid, subtasks=[]),
                ):
                    with pytest.raises(OperationalError) as blocked:
                        update_settings()
                    assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            finally:
                proceed.set()
            submission = future.result(timeout=10)
        assert submission.scoring_mode == "ioi"
        with pytest.raises(AppError) as locked:
            store.update_contest_settings(c.cid, scoring_mode="icpc")
        assert locked.value.code == "scoring_mode_locked"
    finally:
        proceed.set()
        event.remove(ioi_database, "before_cursor_execute", before_query)
        event.remove(ioi_database, "after_cursor_execute", after_query)


def test_icpc_claim_reconciles_stale_pending_ioi_metadata(context):
    c = context
    sid, _ = create(c, claim=False)
    with c.sessions() as db:
        db.get(ContestRow, c.cid).scoring_mode = "icpc"
        row = db.get(SubmissionRow, sid)
        row.score, row.max_score = 30, 100
        row.subtask_results = [{"subtask_id": "small", "score": 30}]
        row.scoring_snapshot = {"testcase_orders": [1, 2]}
        row.scoring_evidence = [{"index": 1, "final_status": "accepted"}]
        db.commit()
    job = store.claim_jobs(c.node.judge_node_id, SECRET, 8)[0]
    assert job["scoring_mode"] == "icpc"
    result, _ = store.report_judge_result(job["judge_job_id"], SECRET, job["lease_token"],
        SubmissionStatus.ACCEPTED, None, None, None)
    assert result.scoring_mode == "icpc" and result.score is None
    assert result.max_score is None and result.subtask_results == []
    with c.sessions() as db:
        row = db.get(SubmissionRow, sid)
        assert row.scoring_snapshot == {} and row.scoring_evidence == []
