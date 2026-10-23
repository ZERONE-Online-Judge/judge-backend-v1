from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool

from app.services import presentation_access
from app.services.access_logging import write_access_log
from app.services.authz import bearer_token, require_contest_staff
from app.services.presentation_board import presentation_board
from app.services.scoreboard_events import scoreboard_events
from app.services.responses import ok

router = APIRouter(tags=["presentation"])


class PresentationLoginRequest(BaseModel):
    email: EmailStr


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"


@router.get("/operator/contests/{contest_id}/scoreboard/presentation-account")
async def get_account(contest_id: str, request: Request, response: Response):
    require_contest_staff(request, contest_id, "contest.scoreboard.manage")
    private_response(response)
    return ok(request, presentation_access.get_account(contest_id))


@router.put("/operator/contests/{contest_id}/scoreboard/presentation-account")
async def issue_account(contest_id: str, request: Request, response: Response):
    require_contest_staff(request, contest_id, "contest.scoreboard.manage")
    private_response(response)
    return ok(request, presentation_access.issue_account(contest_id))


@router.delete("/operator/contests/{contest_id}/scoreboard/presentation-account")
async def revoke_account(contest_id: str, request: Request):
    require_contest_staff(request, contest_id, "contest.scoreboard.manage")
    presentation_access.revoke_account(contest_id)
    return ok(request, {"revoked": True})


@router.post("/auth/presentation/login")
async def presentation_login(
    payload: PresentationLoginRequest, request: Request, response: Response
):
    private_response(response)
    session = presentation_access.login(
        str(payload.email),
        request.headers.get("x-real-ip")
        or (request.client.host if request.client else "unknown"),
    )
    # Never log the alias: it is the credential, not an ordinary account email.
    write_access_log(
        request,
        event_type="presentation_login",
        account_scope="presentation",
        contest_id=session["contest_id"],
        actor_role="presentation",
    )
    return ok(request, session)


@router.get("/presentation/contests/{contest_id}/scoreboard")
def display_scoreboard(
    contest_id: str,
    request: Request,
    response: Response,
    division_id: list[str] | None = Query(default=None, max_length=100),
):
    private_response(response)
    presentation_access.require_presentation(bearer_token(request), contest_id)
    return ok(
        request,
        presentation_board(contest_id, display_only=True, division_ids=division_id),
    )


@router.get("/presentation/contests/{contest_id}/scoreboard/events")
async def display_events(contest_id: str, request: Request):
    def authorize():
        presentation_access.require_presentation(bearer_token(request), contest_id)

    await run_in_threadpool(authorize)
    return StreamingResponse(
        scoreboard_events(request, contest_id, authorize),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/operator/contests/{contest_id}/scoreboard/presentation/events")
async def operator_events(contest_id: str, request: Request):
    def authorize():
        require_contest_staff(request, contest_id, "contest.scoreboard.view")

    await run_in_threadpool(authorize)
    return StreamingResponse(
        scoreboard_events(request, contest_id, authorize),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
