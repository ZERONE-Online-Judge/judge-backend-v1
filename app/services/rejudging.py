"""Staged judging: immutable inputs and evidence, explicit atomic score updates."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import hashlib
import json

from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import JUDGE_FINAL_STATUSES, Submission, new_id, now_utc
from app.orm_models import (
    BundleWarmQueueItemRow, JudgeJobRow, ProblemAssetRow, ProblemRow, RejudgingItemRow as Item,
    RejudgingRow as Batch, SubmissionRow, TestcaseRow, TestcaseSetRow,
    VerificationRunRow, VerificationTrialRow,
)
from app.services.errors import AppError
from app.services.storage import object_storage

RESULT_FIELDS = (
    "status", "compile_message", "judge_message", "failed_testcase_order",
    "runtime_ms", "memory_kb", "progress_current", "progress_total", "status_updated_at",
)
FINAL = {value.value for value in JUDGE_FINAL_STATUSES}


def conflict(message):
    raise AppError(409, "rejudging_conflict", message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def result_snapshot(row):
    values = {name: getattr(row, name) for name in RESULT_FIELDS}
    for name in ("status_updated_at",):
        value = values[name]
        if value is not None and value.tzinfo is None:
            values[name] = value.replace(tzinfo=timezone.utc)
    return jsonable_encoder(values)


def context_snapshot(db, problem_id):
    from app.services.store import _problem, _testcase, _testcase_set, _asset
    # Serialize metadata changes through the decision's commit. Parent row
    # locks also prevent child inserts via PostgreSQL foreign-key checks.
    problem = db.scalar(select(ProblemRow).where(ProblemRow.problem_id == problem_id).with_for_update())
    if problem is None:
        raise AppError(404, "not_found", "문제를 찾을 수 없습니다.")
    active = db.scalar(select(TestcaseSetRow).where(
        TestcaseSetRow.problem_id == problem_id, TestcaseSetRow.is_active.is_(True),
    ).with_for_update())
    cases = list(db.scalars(select(TestcaseRow).where(
        TestcaseRow.testcase_set_id == active.testcase_set_id,
    ).order_by(TestcaseRow.display_order, TestcaseRow.testcase_id).with_for_update())) if active else []
    assets = list(db.scalars(select(ProblemAssetRow).where(
        ProblemAssetRow.problem_id == problem_id,
        ProblemAssetRow.storage_key.contains("/package-files/"),
    ).order_by(ProblemAssetRow.asset_id).with_for_update()))
    files = []
    for asset in assets:
        for role in ("package-resource", "checker", "validator"):
            if f"/package-files/{role}/" in asset.storage_key:
                files.append({**_asset(asset).model_dump(mode="json"), "role": role})
                break
    context = {
        "problem": _problem(problem).model_dump(mode="json"),
        "testcase_set": _testcase_set(active).model_dump(mode="json") if active else None,
        "testcases": [_testcase(case).model_dump(mode="json") for case in cases],
        "package_files": files,
    }
    return context, problem, active, cases


def context_digest(context):
    # Editorial/display edits do not invalidate actual judge evidence.
    return digest({
        "problem": {name: context["problem"].get(name) for name in (
            "problem_id", "time_limit_ms", "memory_limit_mb", "language_resource_limits",
        )},
        "testcase_set_id": (context["testcase_set"] or {}).get("testcase_set_id"),
        "testcases": [{name: case.get(name) for name in (
            "testcase_id", "display_order", "input_storage_key", "output_storage_key",
            "input_sha256", "output_sha256", "time_limit_ms_override", "memory_limit_mb_override",
        )} for case in context["testcases"]],
        "package_files": [{name: asset.get(name) for name in (
            "asset_id", "role", "storage_key", "sha256",
        )} for asset in context["package_files"]],
    })


def item_for_job(db, job_id):
    return db.scalar(select(Item).where(Item.judge_job_id == job_id))


def is_job():
    return select(Item.judge_job_id).where(Item.judge_job_id == JudgeJobRow.judge_job_id).exists()


def active_for_submission(db, submission_id):
    return db.scalar(select(Item.rejudging_item_id).where(Item.active_submission_id == submission_id)) is not None


def active_for_problem(db, problem_id):
    return db.scalar(select(Item.rejudging_item_id).join(Batch, Batch.rejudging_id == Item.rejudging_id)
        .where(Batch.problem_id == problem_id, Item.active_submission_id.is_not(None)).limit(1)) is not None


def create(store, cid, **kwargs):
    try:
        return _create(store, cid, **kwargs)
    except IntegrityError:
        conflict("동시에 같은 제출의 재채점이 요청되었습니다. 목록을 새로고침해 주세요.")


def _create(store, cid, *, submission_id=None, problem_id=None, team_id=None, language=None, reason, actor):
    with store._session() as db:
        if submission_id:
            target = db.get(SubmissionRow, submission_id)
            if not target or target.contest_id != cid:
                raise AppError(404, "not_found", "제출을 찾을 수 없습니다.")
            from app.orm_models import VerificationExecutionRow
            if db.get(VerificationExecutionRow, submission_id):
                conflict("검증 코드는 문제 관리의 ‘검증 코드 채점’에서 다시 채점해 주세요. 전체 테스트의 실행 기록을 새로 저장합니다.")
            problem_id = target.problem_id
        context, problem, active, cases = context_snapshot(db, problem_id)
        if problem.contest_id != cid:
            raise AppError(404, "not_found", "문제를 찾을 수 없습니다.")
        if not active or not cases:
            conflict("활성 테스트케이스를 등록한 뒤 재채점해 주세요.")
        context["bundle_storage_key"] = store._judge_bundle_key(cid, problem, active.testcase_set_id,
            cases, [SimpleNamespace(**asset) for asset in context["package_files"]])
        filters = [SubmissionRow.contest_id == cid, SubmissionRow.problem_id == problem_id]
        if submission_id:
            filters.append(SubmissionRow.submission_id == submission_id)
        else:
            # Bulk rejudging is a participant operation; never overwrite AI evidence.
            filters.append(SubmissionRow.submission_kind == "participant")
        if team_id:
            filters.append(SubmissionRow.participant_team_id == team_id)
        if language:
            filters.append(SubmissionRow.language == language)
        rows = list(db.scalars(select(SubmissionRow).where(*filters)
            .order_by(SubmissionRow.submission_id).with_for_update()
            .execution_options(populate_existing=True)))
        skipped = sum(row.status not in FINAL for row in rows)
        if submission_id and skipped:
            conflict("진행 중인 제출은 채점이 끝난 뒤 다시 요청해 주세요.")
        rows = [row for row in rows if row.status in FINAL]
        if not rows:
            conflict("선택한 범위에 채점이 완료된 참가자 제출이 없습니다.")
        ids = [row.submission_id for row in rows]
        if db.scalar(select(Item.rejudging_item_id).where(Item.active_submission_id.in_(ids)).limit(1)):
            conflict("이미 재채점 중이거나 결과 반영을 기다리는 제출이 있습니다. 기존 작업을 반영하거나 취소해 주세요.")
        if db.scalar(select(JudgeJobRow.judge_job_id).where(
            JudgeJobRow.submission_id.in_(ids), JudgeJobRow.status.in_(["pending", "running"]),
        ).limit(1)):
            conflict("채점 대기 또는 실행 중인 제출이 포함되어 있습니다.")
        if db.scalar(select(VerificationTrialRow.submission_id).where(VerificationTrialRow.submission_id.in_(ids)).limit(1)):
            conflict("AI 내부 실행 기록은 보존됩니다. 검증 요청창에서 다시 실행해 주세요.")
        batch = Batch(rejudging_id=new_id(), contest_id=cid, problem_id=problem_id,
            status="pending", reason=reason.strip(), scope={
                "kind": "submission" if submission_id else "problem",
                "submission_id": submission_id, "team_id": team_id, "language": language,
                "skipped_pending": skipped,
            }, judge_context=context, context_hash=context_digest(context),
            requested_by_name=actor.display_name, requested_by_email=actor.email)
        db.add(batch)
        db.flush()
        if object_storage.size_bytes(context["bundle_storage_key"]) is None and not db.scalar(
            select(BundleWarmQueueItemRow.bundle_warm_queue_id).where(
                BundleWarmQueueItemRow.contest_id == cid, BundleWarmQueueItemRow.problem_id == problem_id,
                BundleWarmQueueItemRow.status.in_(["pending", "running"]),
            ).limit(1)
        ):
            db.add(BundleWarmQueueItemRow(contest_id=cid, problem_id=problem_id, status="pending"))
        position = (db.scalar(select(func.max(JudgeJobRow.queue_position))) or 0) + 1
        verification_hash = None
        for index, row in enumerate(rows):
            run = db.get(VerificationRunRow, row.submission_id)
            previous = result_snapshot(row)
            previous.update({"source_sha256": hashlib.sha256(row.source_code.encode()).hexdigest(), "language": row.language})
            if run:
                previous["verification_context_hash"] = run.context_hash
                previous["analysis_id"] = run.analysis_id
                if verification_hash is None:
                    from app.services.verification_ai import context_metadata, digest as ai_digest, insert_once, Snapshot
                    ai_context = context_metadata(db, problem, active, cases)
                    verification_hash = ai_digest(ai_context)
                    insert_once(db, Snapshot, dict(context_hash=verification_hash, contest_id=cid,
                        problem_id=problem_id, context=ai_context, created_at=now_utc()), "context_hash")
            job = JudgeJobRow(judge_job_id=new_id(), submission_id=row.submission_id,
                contest_id=cid, division_id=row.division_id, status="pending", queue_position=position + index)
            db.add(job)
            db.flush()
            db.add(Item(rejudging_item_id=new_id(), rejudging_id=batch.rejudging_id,
                submission_id=row.submission_id, active_submission_id=row.submission_id,
                judge_job_id=job.judge_job_id, original_result=previous,
                candidate_result={"status": "waiting"}, verification_context_hash=verification_hash if run else None))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            conflict("동시에 같은 제출의 재채점이 요청되었습니다. 목록을 새로고침해 주세요.")
        return batch_summary(db, batch)


def claim_payload(db, job, submission, item):
    """Generate fresh signed URLs from the immutable manifest, not today's tests."""
    batch = db.get(Batch, item.rejudging_id)
    context = deepcopy(batch.judge_context)
    bundle_key = context.pop("bundle_storage_key", None)
    bundle_url = object_storage.internal_presigned_get_url(bundle_key) if (
        bundle_key and object_storage.size_bytes(bundle_key) is not None
    ) else None
    for case in context["testcases"]:
        case["input_url"] = object_storage.internal_presigned_get_url(case["input_storage_key"])
        case["output_url"] = object_storage.internal_presigned_get_url(case["output_storage_key"])
    for asset in context["package_files"]:
        asset["url"] = object_storage.internal_presigned_get_url(asset["storage_key"])
    item.candidate_result = {"status": "preparing", "progress_current": 0,
        "progress_total": len(context["testcases"]), "status_updated_at": now_utc().isoformat()}
    from app.services.store import _job
    return {**_job(job).model_dump(mode="json"), "lease_token": job.lease_token,
        "submission": candidate_submission(submission, item).model_dump(mode="json"),
        **context, "bundle_url": bundle_url}


def candidate_submission(submission, item):
    from app.services.store import _submission
    return Submission.model_validate({**_submission(submission).model_dump(), **item.candidate_result})


def requeue(db, job):
    item = item_for_job(db, job.judge_job_id)
    if item:
        item.candidate_result = {"status": "waiting"}
        return True
    return False


def record_progress(item, status, current, total):
    value = dict(item.candidate_result)
    value.update(status=status.value, progress_total=max(total, 0) if total is not None else None,
        status_updated_at=now_utc().isoformat())
    total = value["progress_total"]
    value["progress_current"] = (0 if total is not None else None) if current is None else (
        min(max(current, 0), total) if total is not None else max(current, 0))
    item.candidate_result = value


def record_result(item, status, compile_message, judge_message, failed_order, runtime_ms, memory_kb):
    value = dict(item.candidate_result)
    value.update(status=status.value, compile_message=compile_message, judge_message=judge_message,
        failed_testcase_order=failed_order, runtime_ms=max(runtime_ms, 0) if runtime_ms is not None else None,
        memory_kb=max(memory_kb, 0) if memory_kb is not None else None, status_updated_at=now_utc().isoformat())
    if status.value == "accepted" and value.get("progress_total") is not None:
        value["progress_current"] = value["progress_total"]
    item.candidate_result = value


def batch_summary(db, batch):
    # Aggregate in SQL so polling never transfers all source/result messages.
    from sqlalchemy import case
    state = Item.candidate_result["status"].as_string()
    old = Item.original_result["status"].as_string()
    counts = db.execute(select(func.count(),
        func.sum(case((state.in_(FINAL), 1), else_=0)),
        func.sum(case((state == "system_error", 1), else_=0)),
        func.sum(case((state.in_(FINAL) & (state != old), 1), else_=0)),
    ).where(Item.rejudging_id == batch.rejudging_id)).one()
    total, completed, errors, changed = (int(value or 0) for value in counts)
    status = batch.status
    if status == "pending" and completed == total:
        status = "blocked" if errors else "ready"
    return jsonable_encoder({name: getattr(batch, name) for name in (
        "rejudging_id", "contest_id", "problem_id", "reason", "scope", "requested_by_name",
        "requested_by_email", "decided_by_email", "created_at", "decided_at",
    )} | {"status": status, "total": total, "completed": completed, "system_errors": errors,
        "changed_verdicts": changed, "problem_code": batch.judge_context["problem"]["problem_code"],
        "problem_title": batch.judge_context["problem"]["title"]})


def list_batches(store, cid, offset=0, limit=20):
    with store._session() as db:
        total = db.scalar(select(func.count()).select_from(Batch).where(Batch.contest_id == cid)) or 0
        rows = db.scalars(select(Batch).where(Batch.contest_id == cid).order_by(Batch.created_at.desc(), Batch.rejudging_id.desc()).offset(offset).limit(limit))
        return {"batches": [batch_summary(db, row) for row in rows], "total": total}


def get_batch(db, cid, bid):
    batch = db.get(Batch, bid)
    if not batch or batch.contest_id != cid:
        raise AppError(404, "not_found", "재채점 작업을 찾을 수 없습니다.")
    return batch


def detail(store, cid, bid, offset=0, limit=50):
    with store._session() as db:
        batch = get_batch(db, cid, bid)
        rows = db.execute(select(Item, SubmissionRow).join(SubmissionRow, Item.submission_id == SubmissionRow.submission_id)
            .where(Item.rejudging_id == bid).order_by(SubmissionRow.submitted_at, Item.submission_id).offset(offset).limit(limit))
        items = [{"submission_id": submission.submission_id, "language": submission.language,
            "submitted_at": submission.submitted_at, "submission_kind": submission.submission_kind,
            "original_result": item.original_result, "candidate_result": item.candidate_result,
            "changed": item.candidate_result.get("status") in FINAL and item.original_result["status"] != item.candidate_result["status"],
        } for item, submission in rows]
        summary = batch_summary(db, batch)
        return {**summary, "items": jsonable_encoder(items), "offset": offset, "limit": limit,
            "has_next": offset + limit < summary["total"]}


def decide(store, cid, bid, action, actor):
    with store._session() as db:
        # Same job-first order as claim/result/lease recovery/credential rotation.
        jobs = list(db.scalars(select(JudgeJobRow).join(Item, Item.judge_job_id == JudgeJobRow.judge_job_id)
            .join(Batch, Batch.rejudging_id == Item.rejudging_id)
            .where(Batch.rejudging_id == bid, Batch.contest_id == cid)
            .order_by(JudgeJobRow.judge_job_id).with_for_update(of=JudgeJobRow)))
        batch = db.scalar(select(Batch).where(Batch.rejudging_id == bid, Batch.contest_id == cid).with_for_update())
        if not batch:
            raise AppError(404, "not_found", "재채점 작업을 찾을 수 없습니다.")
        if batch.status != "pending":
            conflict("이미 반영하거나 취소한 작업입니다.")
        # Compare-and-set also protects SQLite, whose row locks are ignored.
        claimed = db.execute(update(Batch).where(Batch.rejudging_id == bid, Batch.status == "pending")
            .values(status="applied" if action == "apply" else "cancelled", decided_by_email=actor.email, decided_at=now_utc()))
        if claimed.rowcount != 1:
            conflict("다른 운영자가 이미 처리한 작업입니다.")
        items = list(db.scalars(select(Item).where(Item.rejudging_id == bid).order_by(Item.submission_id)))
        if action == "apply":
            if any(item.candidate_result.get("status") not in FINAL or item.candidate_result.get("status") == "system_error" for item in items):
                conflict("모든 재채점이 끝나고 시스템 에러가 없어야 결과를 반영할 수 있습니다.")
            context, _, _, _ = context_snapshot(db, batch.problem_id)
            if context_digest(context) != batch.context_hash:
                conflict("테스트·체커·자원 제한이 변경되었습니다. 이 작업을 취소하고 다시 재채점해 주세요.")
            from app.services.rejudging_notifications import create_for_change
            for item in items:
                submission = db.scalar(select(SubmissionRow).where(SubmissionRow.submission_id == item.submission_id).with_for_update())
                old = item.original_result
                if submission is None or result_snapshot(submission) != {name: old.get(name) for name in RESULT_FIELDS} or (
                    hashlib.sha256(submission.source_code.encode()).hexdigest() != old["source_sha256"] or submission.language != old["language"]
                ):
                    conflict("기존 제출 결과가 변경되었습니다. 새 결과를 덮어쓰지 않고 작업을 중단합니다.")
                for name in RESULT_FIELDS:
                    value = item.candidate_result.get(name)
                    setattr(submission, name, datetime.fromisoformat(value) if name == "status_updated_at" and value else value)
                submission.status_updated_at = now_utc()
                create_for_change(db, batch, item, submission, context)
                run = db.get(VerificationRunRow, item.submission_id)
                if run:
                    run.context_hash = item.verification_context_hash
                    run.analysis_id = None
        else:
            for job in jobs:
                if job.status in {"pending", "running"}:
                    job.status = "failed"
                    job.lease_token = None
                    job.leased_at = None
            for item in items:
                if item.candidate_result.get("status") not in FINAL:
                    item.candidate_result = {**item.candidate_result, "status": "cancelled",
                        "status_updated_at": now_utc().isoformat()}
        for item in items:
            item.active_submission_id = None
        db.commit()
        db.refresh(batch)
        return batch_summary(db, batch)


def delete_problem_history(db, pid):
    batch_ids = select(Batch.rejudging_id).where(Batch.problem_id == pid)
    db.execute(delete(Item).where(Item.rejudging_id.in_(batch_ids)))
    db.execute(delete(Batch).where(Batch.problem_id == pid))
