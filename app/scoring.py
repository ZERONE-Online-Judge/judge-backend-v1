"""IOI fixed-score subtasks: trusted configuration and server-derived evidence.

A submission earns a subtask only when every member test passes. Official
problem scores add the best result for each subtask across submissions.
"""
from copy import deepcopy
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select

from app.services.errors import AppError, TestcaseResultsInvalid
from app.judge_timing import timing_evidence

CAPABILITY = "zoj-judge-subtasks-v1"
SCORING_MODES = ("icpc", "ioi")
FINAL = {"accepted", "wrong_answer", "compile_error", "runtime_error", "time_limit_exceeded",
         "memory_limit_exceeded", "output_limit_exceeded", "system_error"}


class Subtask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subtask_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    points: StrictInt = Field(ge=1, le=100)
    testcase_orders: list[Annotated[StrictInt, Field(ge=1, le=1_000_000)]] = Field(min_length=1, max_length=10000)

    @field_validator("testcase_orders")
    @classmethod
    def unique_orders(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("서브태스크에 같은 테스트 번호를 중복 지정할 수 없습니다.")
        return sorted(value)


def invalid(message):
    raise AppError(422, "subtasks_invalid", message)


def normalize_subtasks(values, orders=None):
    if not isinstance(values, list) or len(values) > 100:
        invalid("서브태스크는 최대 100개까지 지정할 수 있습니다.")
    try:
        result = [Subtask.model_validate(item).model_dump() for item in values]
    except ValueError as error:
        invalid(str(error))
    if not result:
        return []
    if len({item["subtask_id"] for item in result}) != len(result):
        invalid("서브태스크 ID는 서로 달라야 합니다.")
    if sum(item["points"] for item in result) != 100:
        invalid("서브태스크 배점 합계는 정확히 100점이어야 합니다.")
    if orders is not None:
        known = set(orders)
        referenced = {order for item in result for order in item["testcase_orders"]}
        if known != referenced:
            invalid("모든 채점 테스트를 서브태스크에 포함하고, 존재하는 테스트 번호만 지정하세요.")
    return result


def mode_locked(db, contest_id):
    from app.orm_models import SubmissionRow, ScoreboardReleaseRow
    return bool(db.scalar(select(SubmissionRow.submission_id).where(
        SubmissionRow.contest_id == contest_id).limit(1)) or db.scalar(
        select(ScoreboardReleaseRow.division_id).where(ScoreboardReleaseRow.contest_id == contest_id,
            ScoreboardReleaseRow.mode != "not_started").limit(1)))


def subtasks_locked(db, problem_id):
    from app.orm_models import SubmissionRow
    return bool(db.scalar(select(SubmissionRow.submission_id).where(
        SubmissionRow.problem_id == problem_id, SubmissionRow.submission_kind == "participant").limit(1)))


def signature(groups):
    # Member tests may change through explicit rejudging, but stable group IDs
    # and weights must never silently combine incompatible score definitions.
    return sorted((item["subtask_id"], item["points"],
        tuple(sorted(item.get("testcase_orders", []))) if item["subtask_id"] != "__all__" else ()) for item in groups)


def ensure_compatible(db, problem_id, subtasks):
    from app.orm_models import SubmissionRow, TestcaseSetRow
    if not subtasks_locked(db, problem_id):
        return
    snapshots = db.scalars(select(SubmissionRow.scoring_snapshot).where(
        SubmissionRow.problem_id == problem_id, SubmissionRow.submission_kind == "participant",
        SubmissionRow.scoring_mode == "ioi")).all()
    target = signature(subtasks or [{"subtask_id": "__all__", "points": 100}])
    for snapshot in snapshots:
        if snapshot and signature(snapshot["subtasks"]) != target:
            raise AppError(409, "subtasks_locked", "제출이 있는 문제의 서브태스크 ID와 배점은 변경할 수 없습니다. 새 문제로 등록하세요.")
    if not any(snapshots):
        active = db.scalar(select(TestcaseSetRow).where(TestcaseSetRow.problem_id == problem_id,
            TestcaseSetRow.is_active.is_(True)))
        if active and signature(active.subtasks or [{"subtask_id": "__all__", "points": 100}]) != target:
            raise AppError(409, "subtasks_locked", "제출이 있는 문제의 서브태스크 ID와 배점은 변경할 수 없습니다.")


def snapshot(testcase_set, cases):
    orders = [case.display_order if hasattr(case, "display_order") else case["display_order"] for case in cases]
    if not orders or len(set(orders)) != len(orders):
        invalid("채점할 테스트가 없거나 테스트 번호가 중복되어 점수를 계산할 수 없습니다.")
    groups = normalize_subtasks(getattr(testcase_set, "subtasks", None) or [], orders)
    if not groups:
        groups = [{"subtask_id": "__all__", "name": "전체 테스트", "points": 100, "testcase_orders": sorted(orders)}]
    value = {"version": 1, "testcase_set_id": getattr(testcase_set, "testcase_set_id", None),
             "testcase_version": getattr(testcase_set, "version", None), "subtasks": groups,
             "testcase_orders": sorted(orders)}
    value["config_hash"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def attach(submission, testcase_set, cases, lease_token):
    submission.scoring_mode = "ioi"
    submission.scoring_snapshot = {**snapshot(testcase_set, cases), "lease_token": lease_token}
    submission.scoring_evidence = []
    submission.score = None
    submission.max_score = 100
    submission.subtask_results = []


def merge_evidence(snapshot_value, previous, incoming):
    expected = set(snapshot_value["testcase_orders"])
    values = {item["index"]: item for item in previous or []}
    seen = set()
    for item in incoming or []:
        index, status = item.get("index"), item.get("final_status")
        if type(index) is not int or index not in expected or index in seen or status not in FINAL or status == "compile_error":
            raise TestcaseResultsInvalid("invalid_testcase")
        seen.add(index)
        try:
            value = {"index": index, "final_status": status, **timing_evidence(item)}
        except ValueError as error:
            raise TestcaseResultsInvalid("invalid_format") from error
        if index in values and values[index] != value:
            raise TestcaseResultsInvalid("testcase_changed")
        values[index] = value
    return [values[index] for index in sorted(values)]


def calculate(snapshot_value, evidence, final_status, failed_order=None):
    groups = snapshot_value["subtasks"]
    if final_status == "compile_error":
        return 0, [{"subtask_id": group["subtask_id"], "name": group["name"], "score": 0,
                    "max_score": group["points"], "status": "compile_error"} for group in groups]
    if final_status == "system_error":
        return None, []
    by_order = {item["index"]: item["final_status"] for item in evidence}
    if set(by_order) != set(snapshot_value["testcase_orders"]):
        raise TestcaseResultsInvalid("incomplete_suite")
    failures = [(index, status) for index, status in sorted(by_order.items()) if status != "accepted"]
    first_order, first_status = failures[0] if failures else (None, "accepted")
    if final_status != first_status or failed_order != first_order:
        raise TestcaseResultsInvalid("aggregate_mismatch")
    if any(status == "system_error" for status in by_order.values()):
        return None, []
    results = []
    for group in groups:
        failure = next((index for index in group["testcase_orders"] if by_order[index] != "accepted"), None)
        result = {"subtask_id": group["subtask_id"], "name": group["name"],
                  "score": group["points"] if failure is None else 0,
                  "max_score": group["points"], "status": by_order[failure] if failure else "accepted"}
        if failure is not None:
            result["failed_testcase_order"] = failure
        results.append(result)
    return sum(item["score"] for item in results), results


def record(submission, incoming, final_status=None, failed_order=None):
    values = merge_evidence(submission.scoring_snapshot, submission.scoring_evidence, incoming)
    if final_status is not None:
        submission.score, submission.subtask_results = calculate(submission.scoring_snapshot, values, final_status, failed_order)
    submission.scoring_evidence = values
    return len(values)


def accumulate(cell, submission):
    """Mutate a scoreboard cell from one official valid submission, no IO leaks."""
    if submission.get("score") is None:
        return
    groups = {item["subtask_id"]: item for item in cell.setdefault("subtask_scores", [])}
    changed = False
    for result in submission.get("subtask_results") or []:
        old = groups.get(result["subtask_id"])
        if old is None or result["score"] > old["score"]:
            groups[result["subtask_id"]] = {key: result[key] for key in ("subtask_id", "name", "score", "max_score")}
            changed |= result["score"] > (old["score"] if old else 0)
    cell["subtask_scores"] = list(groups.values())
    cell["score"] = sum(item["score"] for item in groups.values())
    cell["max_score"] = 100
    cell["solved"] = cell["score"] == 100
    if changed:
        cell["best_submission_id"] = submission.get("submission_id")
        cell["best_submitted_at"] = submission.get("submitted_at")
        cell["score_achieved_at"] = submission.get("submitted_at")
        cell["best_status"] = submission.get("status")
        cell["solved_at"] = submission.get("submitted_at") if cell["solved"] else None


def rank_rows(rows):
    # Names stabilize display only; equal score remains an official tie.
    rows.sort(key=lambda row: (-row["total_score"], row["team_name"]))
    previous, rank = None, 0
    for index, row in enumerate(rows, 1):
        if row["total_score"] != previous:
            previous, rank = row["total_score"], index
        row["rank"] = rank


def project(rows, submissions, problems):
    by_problem = {problem.problem_id: problem for problem in problems}
    for row in rows:
        row["problem_scores"] = [cell for cell in row["problem_scores"]
            if by_problem[cell["problem_id"]].division_id == row["division_id"]]
    groups = {(row["team_id"], cell["problem_id"]): cell for row in rows for cell in row["problem_scores"]}
    for cell in groups.values():
        cell.update(score=0, max_score=100, subtask_scores=[], solved=False, penalty=None,
                    solved_at=None, best_submission_id=None, best_submitted_at=None, best_status=None)
    for submission in submissions:
        cell = groups.get((submission.participant_team_id, submission.problem_id))
        if cell is not None:
            accumulate(cell, {"score": submission.score, "subtask_results": submission.subtask_results,
                "submitted_at": submission.submitted_at, "submission_id": submission.submission_id, "status": submission.status})
    for row in rows:
        row["scoring_mode"] = "ioi"
        row["total_score"] = sum(cell["score"] for cell in row["problem_scores"])
        row["max_score"] = len(row["problem_scores"]) * 100
        row["solved"] = sum(cell["solved"] for cell in row["problem_scores"])
        row["penalty"] = None
        row["last_solved_at"] = None
    rank_rows(rows)
    return rows
