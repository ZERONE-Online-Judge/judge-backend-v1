"""Confirm public changes before saving, using the mutation's own permission."""
import hashlib
import json
from contextvars import ContextVar

from fastapi.routing import APIRoute
from sqlalchemy import select

from app.models import ContestStatus
from app.orm_models import ContestRow, ProblemRow
from app.services.errors import AppError, not_found
from app.services.request_sessions import atomic_request_database

_live_problem = ContextVar("confirmed_live_problem_change", default=None)
FIELD_LABELS = {
    "problem_code": "문제 번호", "title": "제목", "statement": "지문",
    "time_limit_ms": "시간 제한", "memory_limit_mb": "메모리 제한",
    "allowed_languages": "제출 언어", "language_resource_limits": "언어별 제한",
    "statement_translations": "언어별 지문", "default_statement_language": "기본 지문 언어",
    "editorial": "해설", "editorial_translations": "언어별 해설",
    "display_order": "표시 순서", "division_id": "참가 유형",
}


def allows_live_problem_change(contest_id):
    return _live_problem.get() == contest_id


def _brief(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 240 else text[:237] + "…"


async def _request_data(request):
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        data = {}
        for key, value in form.multi_items():
            if key == "_change_notice":
                try:
                    data[key] = json.loads(value)
                except (ValueError, TypeError):
                    data[key] = None
            elif hasattr(value, "file"):
                digest = hashlib.sha256()
                value.file.seek(0)
                while chunk := value.file.read(1024 * 1024):
                    digest.update(chunk)
                value.file.seek(0)
                data[key] = {"filename": value.filename, "sha256": digest.hexdigest()}
            else:
                data[key] = value
        return data
    if not await request.body():
        return {}
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def confirm_change(request, contest, title, body, snapshot):
    data = await _request_data(request)
    confirmation = data.pop("_change_notice", None)
    fingerprint = hashlib.sha256(json.dumps(
        [request.method, request.url.path, request.url.query, data, snapshot],
        sort_keys=True, ensure_ascii=False, default=str,
    ).encode()).hexdigest()
    details = {"title": title, "body": body, "fingerprint": fingerprint}
    notice_title, notice_body = validate_confirmation(confirmation, details)
    request.state.change_notice = (contest.contest_id, notice_title, notice_body)


def validate_confirmation(confirmation, details):
    fingerprint = details["fingerprint"]
    if not isinstance(confirmation, dict) or confirmation.get("fingerprint") != fingerprint:
        raise AppError(409, "change_notice_required", "변경사항을 참가자에게 공지해야 합니다.", details)
    notice_title, notice_body = confirmation.get("title"), confirmation.get("body")
    reason = confirmation.get("reason")
    if not isinstance(notice_title, str) or not notice_title.strip() or len(notice_title) > 200:
        raise AppError(422, "change_notice_invalid", "공지 제목을 200자 이내로 입력해 주세요.")
    if not isinstance(notice_body, str) or not notice_body.strip() or len(notice_body) > 20000:
        raise AppError(422, "change_notice_invalid", "변경 내용과 사유를 20,000자 이내로 입력해 주세요.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise AppError(422, "change_notice_invalid", "변경 사유를 2,000자 이내로 입력해 주세요.")
    return notice_title.strip(), f"{notice_body.strip()}\n\n변경 사유: {reason.strip()}"


def _target(request):
    path = request.scope.get("route").path.removeprefix("/api")
    if request.method == "PATCH" and path == "/operator/contests/{contest_id}/settings":
        return "settings"
    prefix = "/operator/contests/{contest_id}/problems/{problem_id}"
    if request.method == "PATCH" and path == prefix:
        return "problem"
    if request.method in {"POST", "PATCH", "DELETE"} and path.startswith(prefix + "/"):
        suffix = path[len(prefix):]
        if suffix.startswith(("/assets", "/testcase-sets", "/verified-testcase-sets")):
            return "resource"
    return None


async def _confirm_problem(request, contest):
    from app.services.authz import require_contest_staff
    from app.services.store import store
    require_contest_staff(request, contest.contest_id, "contest.problem.manage")
    problem_id = request.path_params["problem_id"]
    problem = store.get_problem(problem_id)
    if not problem or problem.contest_id != contest.contest_id:
        raise not_found()
    if contest.status != ContestStatus.RUNNING:
        return
    data = await _request_data(request)
    if _target(request) == "problem":
        changes = [(key, getattr(problem, key), value) for key, value in data.items()
                   if key in FIELD_LABELS and value is not None and getattr(problem, key) != value]
        if not changes:
            return
        fields = ", ".join(FIELD_LABELS[key] for key, _, _ in changes)
        lines = []
        for key, before, after in changes:
            label = FIELD_LABELS[key]
            # Never publish private solution/editorial sources in a notice.
            if key in {"editorial", "editorial_translations"}:
                lines.append(f"- {label}이 수정되었습니다.")
            else:
                unit = "ms" if key == "time_limit_ms" else "MiB" if key == "memory_limit_mb" else ""
                lines.append(f"- {label}: {_brief(before)}{unit} → {_brief(after)}{unit}")
    else:
        sets = store.testcase_sets_for_problem(contest.contest_id, problem_id)
        fields = "테스트케이스" if "testcase" in request.url.path else "첨부·채점 파일"
        action = {"POST": "추가·교체", "PATCH": "설정 변경", "DELETE": "삭제"}[request.method]
        upload = data.get("file")
        filename = data.get("original_filename") or (upload.get("filename") if isinstance(upload, dict) else None)
        if not isinstance(filename, str):
            filename = None
        asset_id = request.path_params.get("asset_id")
        if asset_id:
            asset = store.get_problem_asset(contest.contest_id, problem_id, asset_id)
            if not asset:
                raise not_found()
            filename = asset.original_filename
        lines = [f"- {fields}: {filename + ' · ' if filename else ''}{action}"]
        set_id = request.path_params.get("testcase_set_id")
        selected = next((item for item in sets if item["testcase_set_id"] == set_id), None)
        if set_id and selected is None:
            raise not_found()
        if selected:
            case_id = request.path_params.get("testcase_id")
            case = next((item for item in selected["testcases"] if item["testcase_id"] == case_id), None)
            if case_id and case is None:
                raise not_found()
            lines[0] = f"- 테스트 세트 v{selected['version']}{' · #' + str(case['display_order']) if case else ''}: {action}"
        if "is_active" in data:
            before = '사용' if selected and selected['is_active'] else '사용 안 함'
            after = '사용' if data['is_active'] else '사용 안 함'
            if selected and request.method == "PATCH" and before == after:
                return
            lines.append(f"- 활성 테스트 세트: {before} → {after}")
        if isinstance(data.get("cases"), list):
            lines.append(f"- 업로드한 테스트: {len(data['cases'])}개")
    snapshot = problem.model_dump(mode="json")
    if _target(request) == "resource":
        snapshot = {"problem": snapshot,
            "assets": [asset.model_dump(mode="json") for asset in store.problem_assets_for_problem(contest.contest_id, problem_id)],
            "tests": sets}
    await confirm_change(request, contest, f"{problem.problem_code}번 문제 {fields} 수정 안내",
                         "\n".join(lines), snapshot)


class ChangeNoticeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            target = _target(request)
            if not target:
                return await original(request)
            from app.services.authz import require_contest_staff
            from app.services.store import store
            cid = request.path_params["contest_id"]
            # Check scope before locking or exposing any change details.
            permissions = ("contest.problem.manage",) if target != "settings" else ()
            account = require_contest_staff(request, cid, *permissions)
            with atomic_request_database() as db:
                row = db.scalar(select(ContestRow).where(ContestRow.contest_id == cid).with_for_update())
                if row is None:
                    raise not_found()
                if target != "settings":
                    db.scalar(select(ProblemRow).where(ProblemRow.problem_id == request.path_params["problem_id"],
                        ProblemRow.contest_id == cid).with_for_update())
                    contest = store.get_contest(cid)
                    await _confirm_problem(request, contest)
                token = _live_problem.set(cid if target != "settings" else None)
                try:
                    response = await original(request)
                    notice = getattr(request.state, "change_notice", None)
                    if notice and response.status_code < 400:
                        store.create_contest_notice(*notice, pinned=True, emergency=True,
                            visibility="participants", created_by_email=str(account.email))
                    return response
                finally:
                    _live_problem.reset(token)

        return handler
