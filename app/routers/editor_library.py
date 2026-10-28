"""Compatibility errors for the retired Judge-owned Editor import flow."""
from urllib.parse import urlencode
from fastapi import APIRouter, Request
from app.services.authz import require_contest_staff
from app.services.errors import AppError

router = APIRouter(prefix="/operator/contests", tags=["Retired Editor import"])


@router.api_route("/{contest_id}/editor-problems", methods=["GET", "POST"], deprecated=True)
@router.api_route("/{contest_id}/editor-problems/{legacy_path:path}", methods=["GET", "POST"], deprecated=True)
def moved_to_editor(contest_id: str, request: Request, legacy_path: str = ""):
    require_contest_staff(request, contest_id, "contest.problem.manage")
    raise AppError(410, "editor_transfer_moved", "문제 가져오기와 업데이트는 Editor로 통합되었습니다. 화면을 새로고침한 뒤 ‘Editor에서 가져오기’를 이용하세요.",
        details={"editor_url": "https://editor.zoj.kr/problems/zoj-transfer?" + urlencode({"contest": contest_id})})
