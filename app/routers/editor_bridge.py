import hashlib
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Query, Request
from sqlalchemy import select
from app.database import SessionLocal
from app.orm_models import ContestDivisionRow, ContestRow, ProblemRow
from app.services.authz import has_contest_permission
from app.services.editor_bridge import identity, staff
from app.services.errors import AppError
from app.services.problem_archive import CHUNK, import_archive
from app.services.responses import ok
from app.routers.problem_archives import _receive
from app.services import package_updates

router = APIRouter(prefix="/editor", tags=["editor integration"])


@router.get("/contests")
def eligible_contests(request: Request):
    email, content_hash = identity(request)
    if content_hash != hashlib.sha256(b"").hexdigest():
        raise AppError(401, "editor_content_mismatch", "요청 내용이 인증 정보와 다릅니다.")
    with SessionLocal() as db:
        account = staff(db, email)
        contests = []
        for row in db.scalars(select(ContestRow).order_by(ContestRow.created_at.desc())):
            if not has_contest_permission(account, row.contest_id, "contest.problem.manage"):
                continue
            start = row.start_at.replace(tzinfo=timezone.utc) if row.start_at.tzinfo is None else row.start_at
            end = row.end_at.replace(tzinfo=timezone.utc) if row.end_at.tzinfo is None else row.end_at
            now = datetime.now(timezone.utc)
            locked = row.status == "running" or (row.status not in {"draft", "schedule_tbd", "ended", "finalized", "archived"} and start <= now < end)
            divisions = [{"id": d.division_id, "name": d.name, "code": d.code} for d in db.scalars(select(ContestDivisionRow).where(ContestDivisionRow.contest_id == row.contest_id).order_by(ContestDivisionRow.display_order))]
            contests.append({"id": row.contest_id, "title": row.title, "status": row.status, "locked": locked, "divisions": divisions})
        return ok(request, {"email": email, "contests": contests})


@router.get("/contests/{contest_id}/problems")
def contest_problems(contest_id: str, request: Request):
    email, content_hash = identity(request)
    if content_hash != hashlib.sha256(b"").hexdigest():
        raise AppError(401, "editor_content_mismatch", "요청 내용이 인증 정보와 다릅니다.")
    with SessionLocal() as db:
        staff(db, email, contest_id)
        rows = db.scalars(select(ProblemRow).where(ProblemRow.contest_id == contest_id)
                          .order_by(ProblemRow.display_order, ProblemRow.problem_code)).all()
        return ok(request, {"problems": [{"id": row.problem_id, "title": row.title,
            "code": row.problem_code, "division_id": row.division_id} for row in rows]})


@router.post("/contests/{contest_id}/imports")
async def receive_problem(contest_id: str, request: Request,
        division_id: str = Query(min_length=1, max_length=36),
        problem_code: str = Query(min_length=1, max_length=16),
        operation_id: str = Query(pattern=r"^[a-f0-9]{32}$")):
    email, content_hash = identity(request)
    with SessionLocal() as db:
        staff(db, email, contest_id)
    operation_key = hashlib.sha256((email + "\n" + operation_id).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([contest_id, division_id, problem_code.strip(), content_hash], ensure_ascii=False).encode()).hexdigest()
    context = {"email": email, "operation_key": operation_key, "request_digest": fingerprint, "request_id": request.state.request_id}
    def handle(source):
        digest = hashlib.sha256()
        while chunk := source.read(CHUNK):
            digest.update(chunk)
        if digest.hexdigest() != content_hash:
            raise AppError(401, "editor_content_mismatch", "업로드 내용이 인증 정보와 다릅니다.")
        source.seek(0)
        return import_archive(source, contest_id, division_id, problem_code, editor_context=context)
    return ok(request, await _receive(request, handle))


@router.post("/contests/{contest_id}/problems/{problem_id}/package-updates:preview")
async def preview_update(contest_id: str, problem_id: str, request: Request):
    email, content_hash = identity(request)
    with SessionLocal() as db:
        staff(db, email, contest_id)
    def handle(source):
        if package_updates.archive_sha(source) != content_hash:
            raise AppError(401, "editor_content_mismatch", "패키지 서명이 다릅니다.")
        return package_updates.preview(source, contest_id, problem_id)
    return ok(request, await _receive(request, handle))


@router.post("/contests/{contest_id}/problems/{problem_id}/package-updates")
async def apply_update(contest_id: str, problem_id: str, request: Request,
        target_hash: str = Query(pattern=r"^[a-f0-9]{64}$"),
        reason: str = Query(min_length=1, max_length=1000), confirm_running: bool = False,
        editor_problem_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
        editor_revision: int | None = Query(default=None, ge=0)):
    email, content_hash = identity(request)
    with SessionLocal() as db:
        staff(db, email, contest_id)
    if not reason.strip():
        raise AppError(422, "package_update_reason_required", "업데이트 이유를 입력하세요.")
    operation = package_updates.digest([email, contest_id, problem_id, target_hash, content_hash])
    context = {"email": email, "operation_key": operation, "request_id": request.state.request_id,
               "request_digest": package_updates.digest([target_hash, content_hash, reason, confirm_running]),
               "source": {"problem_id": editor_problem_id, "revision": editor_revision, "sha256": content_hash}}
    def handle(source):
        return package_updates.apply(source, contest_id, problem_id, target_hash, content_hash,
                                     reason.strip(), confirm_running, context)
    result = await _receive(request, handle)
    return ok(request, {"problem": result, "backup_url": f"/api/operator/contests/{contest_id}/problems/{problem_id}/package-backups/{operation}",
                        "official_results_unchanged": True})
