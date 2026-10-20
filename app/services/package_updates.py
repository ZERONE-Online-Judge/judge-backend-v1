"""Preview and apply verified Editor packages without replacing submissions."""
import hashlib
import json
import copy
from datetime import timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Problem, now_utc
from app.orm_models import (ContestRow, ContestDivisionRow, ProblemAssetRow, ProblemRow, RejudgingRow,
                            SubmissionRow, TestcaseRow, TestcaseSetRow)
from app.services.errors import AppError, not_found

FIELDS = {
    "title": "제목", "statement": "지문", "editorial": "해설",
    "default_statement_language": "기본 지문 언어",
    "statement_translations": "번역 지문", "editorial_translations": "번역 해설",
    "time_limit_ms": "시간 제한", "memory_limit_mb": "메모리 제한",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str).encode()).hexdigest()


def archive_sha(source):
    source.seek(0)
    value = hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        value.update(chunk)
    source.seek(0)
    return value.hexdigest()


def running(contest):
    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return contest.status == "running" or (contest.status not in {
        "draft", "schedule_tbd", "ended", "finalized", "archived"
    } and aware(contest.start_at) <= now_utc() < aware(contest.end_at))


def target(db, contest_id, problem_id, lock=False):
    query = select(ProblemRow).where(ProblemRow.contest_id == contest_id,
                                   ProblemRow.problem_id == problem_id)
    row = db.scalar(query.with_for_update() if lock else query)
    contest = db.get(ContestRow, contest_id)
    if not row or not contest or contest.deleted_at is not None:
        raise not_found()
    return row


def snapshot(db, problem, lock=False):
    def rows(query):
        return list(db.scalars(query.with_for_update() if lock else query))
    sets = rows(select(TestcaseSetRow).where(
        TestcaseSetRow.problem_id == problem.problem_id).order_by(TestcaseSetRow.version))
    assets = rows(select(ProblemAssetRow).where(
        ProblemAssetRow.problem_id == problem.problem_id).order_by(ProblemAssetRow.asset_id))
    cases = rows(select(TestcaseRow).join(TestcaseSetRow).where(
        TestcaseSetRow.problem_id == problem.problem_id, TestcaseSetRow.is_active.is_(True)
    ).order_by(TestcaseRow.display_order, TestcaseRow.testcase_id))
    state = {"problem": Problem.model_validate(problem, from_attributes=True).model_dump(mode="json"),
        "sets": [(s.testcase_set_id, s.version, s.is_active) for s in sets],
        "assets": [(a.asset_id, a.storage_key, a.sha256, a.asset_status, a.source_language) for a in assets],
        "cases": [(c.testcase_id, c.display_order, c.input_sha256, c.output_sha256,
                   c.time_limit_ms_override, c.memory_limit_mb_override) for c in cases]}
    return state, assets, sets, cases


def require_verified(manifest, archive):
    item = next((a for a in manifest.assets if a.original_filename == "editor-project.json"
                 and a.category == "package-files/package-resource"), None)
    try:
        file = next(f for f in manifest.files if item and f.path == item.file)
        if file.size > 16 * 1024 * 1024:
            raise ValueError()
        evidence = json.loads(archive.read(item.file))
        if evidence.get("format") != "zoj-editor-source-v1" or evidence.get("verified") is not True:
            raise ValueError()
    except (StopIteration, ValueError, TypeError, AttributeError):
        raise AppError(409, "editor_package_not_verified", "검증을 완료한 Editor 패키지만 ZOJ에 반영할 수 있습니다.")


def preview(source, contest_id, problem_id):
    from app.services.problem_archive import read_archive, _category, rewrite_problem_documents
    sha = archive_sha(source)
    with read_archive(source) as (archive, manifest), SessionLocal() as db:
        require_verified(manifest, archive)
        problem = target(db, contest_id, problem_id)
        state, assets, sets, cases = snapshot(db, problem)
        active = [s for s in manifest.testcase_sets if s.is_active]
        if len(active) != 1 or not active[0].testcases:
            raise AppError(409, "package_update_no_tests", "업데이트할 패키지에 활성 테스트가 있어야 합니다.")
        old_files = {(a.original_filename, _category(a.storage_key)): a.sha256 for a in assets}
        hashes = {f.path: f.sha256 for f in manifest.files}
        new_files = {(a.original_filename, a.category): hashes[a.file] for a in manifest.assets}
        # Archive-local asset IDs must not make untouched statements look edited.
        before, after = copy.deepcopy(state["problem"]), manifest.problem.model_dump(mode="json")
        old_ids = {a.asset_id: 'sha-' + a.sha256 for a in assets}
        rewrite_problem_documents(before, old_ids, {a.storage_key: old_ids[a.asset_id] for a in assets})
        rewrite_problem_documents(after, {a.id: 'sha-' + hashes[a.file] for a in manifest.assets})
        def display(value):
            text = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else str(value)
            return text[:2000]
        changes = [{"field": key, "label": label, "changed": before[key] != after[key],
                    "before": display(before[key]), "after": display(after[key]),
                    "truncated": len(str(before[key])) > 2000 or len(str(after[key])) > 2000}
                   for key, label in FIELDS.items()]
        old_cases = {c.display_order: (c.input_sha256, c.output_sha256, c.time_limit_ms_override, c.memory_limit_mb_override) for c in cases}
        new_cases = {c.display_order: (hashes[c.input], hashes[c.output], c.time_limit_ms_override, c.memory_limit_mb_override) for c in active[0].testcases}
        testcase_changes = {"added": len(new_cases.keys() - old_cases.keys()),
            "removed": len(old_cases.keys() - new_cases.keys()),
            "changed": sum(old_cases[k] != new_cases[k] for k in old_cases.keys() & new_cases.keys())}
        files = [{"filename": name, "category": category,
                  "change": "added" if (name, category) not in old_files else "removed" if (name, category) not in new_files else "changed"}
                 for name, category in sorted(old_files.keys() | new_files.keys())
                 if old_files.get((name, category)) != new_files.get((name, category))]
        return {"target_problem_id": problem_id, "problem_code": problem.problem_code,
            "contest_title": db.get(ContestRow, contest_id).title,
            "division_name": db.get(ContestDivisionRow, problem.division_id).name,
            "title": problem.title, "target_hash": digest(state), "archive_sha256": sha,
            "changes": changes, "files": files[:100], "changed_file_count": len(files),
            "current_testcase_count": len(cases), "incoming_testcase_count": len(active[0].testcases),
            "testcase_changes": testcase_changes,
            "next_testcase_version": max((s.version for s in sets), default=0) + 1,
            "contest_running": running(db.get(ContestRow, contest_id)),
            "submission_count": db.scalar(select(func.count()).select_from(SubmissionRow).where(SubmissionRow.problem_id == problem_id)),
            "preserved": "문제 ID·번호·참가 유형·순서·사용 언어·언어별 제한·기존 제출과 판정을 유지합니다. 기존 테스트는 비활성 버전으로 보관하고 새 테스트를 활성화합니다."}


def apply(source, contest_id, problem_id, expected_target_hash, expected_archive_sha256,
          reason, confirm_running, editor_context, *, change_notice=None, require_notice=False):
    from app.services.problem_archive import import_archive
    if archive_sha(source) != expected_archive_sha256:
        raise AppError(409, "package_update_archive_changed", "확인한 패키지가 변경되었습니다. 변경 내용을 다시 확인하세요.")
    with SessionLocal() as db:
        problem = target(db, contest_id, problem_id)
        division, code = problem.division_id, problem.problem_code
    return import_archive(source, contest_id, division, code, editor_context=editor_context,
        update_context={"problem_id": problem_id, "target_hash": expected_target_hash,
                        "reason": reason, "confirm_running": confirm_running,
                        "archive_hash": expected_archive_sha256,
                        "change_notice": change_notice, "require_notice": require_notice})


def update_notice(contest, problem, manifest, values):
    if not running(contest):
        return None
    from app.services.change_notices import _brief, validate_confirmation
    labels, lines = [], []
    for field, label in FIELDS.items():
        before, after = getattr(problem, field), getattr(manifest.problem, field)
        if before == after:
            continue
        labels.append(label)
        if field in {"editorial", "editorial_translations"}:
            lines.append(f"- {label}이 수정되었습니다.")
        else:
            lines.append(f"- {label}: {_brief(before)} → {_brief(after)}")
    labels.append("테스트·채점 파일")
    lines.append(f"- 테스트·채점 파일: 새 패키지로 교체 · {sum(len(s.testcases) for s in manifest.testcase_sets if s.is_active)}개 테스트")
    draft = {"title": f"{problem.problem_code}번 문제 {', '.join(labels[:3])}{' 외' if len(labels) > 3 else ''} 수정 안내",
        "body": "\n".join(lines), "reason": values["reason"],
        "fingerprint": digest([problem.problem_id, values["target_hash"], values["archive_hash"]])}
    # Signed Editor transfers already include a reason and explicit live-update
    # confirmation. The ZOJ operator UI confirms editable notice copy as well.
    confirmation = values.get("change_notice") if values.get("require_notice") else draft
    return validate_confirmation(confirmation, draft)


def check_update(db, contest, problem, values):
    if digest(snapshot(db, problem, lock=True)[0]) != values["target_hash"]:
        raise AppError(409, "package_update_stale", "대회 문제가 변경되었습니다. 변경 내용을 다시 확인하세요.")
    if running(contest) and not values["confirm_running"]:
        raise AppError(409, "package_update_running_confirmation", "진행 중 대회에 적용하는 것을 확인해 주세요.")
    if db.scalar(select(RejudgingRow.rejudging_id).where(
        RejudgingRow.problem_id == problem.problem_id, RejudgingRow.status == "pending").limit(1)):
        raise AppError(409, "package_update_rejudging_pending", "진행 중인 재채점을 반영하거나 취소한 뒤 업데이트하세요.")
