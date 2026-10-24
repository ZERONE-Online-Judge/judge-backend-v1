from app.services.request_sessions import request_database
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr, Field

from app.models import now_utc
from app.settings import settings
from app.services.authz import bearer_token
from app.services.contest_visibility import contest_payload_for_view, visible_private_contest_ids, contest_has_ended
from app.services.errors import not_found
from app.services.mail_templates import absolute_url, format_korean_datetime, render_branded_email
from app.services.responses import ok, page
from app.services.store import store

router = APIRouter(tags=["public"])


def _contest_payload(contest, request: Request) -> dict:
    payload = contest_payload_for_view(contest)
    if contest.emergency_notice and payload["emergency_notice"] is None:
        token = bearer_token(request)
        participant = (
            store.get_participant_by_access_token(contest.contest_id, token)
            or store.get_participant_by_general_access_token(contest.contest_id, token)
        ) if token else None
        if participant:
            return contest_payload_for_view(contest, participant)
    return payload


class ContactInquiryCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    sender_name: str = Field(min_length=1, max_length=120)
    sender_email: EmailStr
    body: str = Field(min_length=1, max_length=10000)


def _service_master_emails() -> list[str]:
    emails = []
    seen = set()
    for account in store.staff_accounts.values():
        if not account.is_service_master:
            continue
        email = str(account.email).strip().lower()
        if email and email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


@router.get("/public/share/{code}")
def resolve_contest_share(code: str, request: Request, response: Response):
    from app.services.contest_sharing import resolve
    response.headers["Cache-Control"] = "no-store"
    return ok(request, {"contest_id": resolve(code)})


@router.get("/public/home")
@request_database
def home(request: Request):
    contests = store.visible_public_contests()
    emergency = next((notice for notice in store.service_notices.values() if notice.emergency), None)
    return ok(
        request,
        {
            "hero": {
                "title": "Zerone Online Judge",
                "subtitle": "대회 운영, 제출, 채점 큐, 스코어보드를 한 흐름으로 관리합니다.",
            },
            "active_contest_count": len(contests),
            "emergency_notice": emergency.model_dump(mode="json") if emergency else None,
        },
    )


@router.get("/public/contests")
@request_database
def contests(request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    allowed_ids = visible_private_contest_ids(bearer_token(request))
    return page(request, [_contest_payload(contest, request) for contest in store.visible_public_contests(allowed_ids)])


@router.get("/public/contests/{contest_id}")
@request_database
def contest_detail(contest_id: str, request: Request, response: Response):
    allowed_ids = visible_private_contest_ids(bearer_token(request))
    contest = store.get_public_contest(contest_id, allow_private=contest_id in allowed_ids)
    if not contest:
        raise not_found()
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    return ok(
        request,
        {
            "contest": _contest_payload(contest, request),
            "divisions": [division.model_dump(mode="json") for division in store.contest_divisions(contest_id)],
            **({} if (
                contest.hide_participation_counts_before_start
                and not contest_has_ended(contest)
                and now_utc() < contest.start_at
            ) else store.contest_participation_counts(contest_id)),
        },
    )


@router.get("/public/service-notices")
@request_database
def service_notices(request: Request):
    notices = [notice.model_dump(mode="json") for notice in store.service_notices.values()]
    notices.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    return page(request, notices)


@router.get("/public/service-notices/{notice_id}")
@request_database
def service_notice_detail(notice_id: str, request: Request):
    notice = store.service_notices.get(notice_id)
    if not notice:
        raise not_found()
    return ok(request, notice.model_dump(mode="json"))


@router.post("/public/contact-inquiries")
def create_contact_inquiry(payload: ContactInquiryCreateRequest, request: Request):
    inquiry = store.create_contact_inquiry(
        payload.title.strip(),
        payload.sender_name.strip(),
        str(payload.sender_email).strip(),
        payload.body.strip(),
    )
    subject = f"[ZOJ] 서비스 문의 접수: {inquiry.title}"
    received_at = f"{format_korean_datetime(inquiry.created_at)} KST"
    inquiry_url = absolute_url("/admin/inquiries")
    body_text = "\n".join(
        [
            "서비스 문의가 접수되었습니다.",
            "",
            f"문의 ID: {inquiry.contact_inquiry_id}",
            f"제목: {inquiry.title}",
            f"이름: {inquiry.sender_name}",
            f"이메일: {inquiry.sender_email}",
            f"접수 시각: {received_at}",
            "",
            "문의 본문:",
            inquiry.body,
            "",
            "서비스 관리자 페이지에서 답변을 등록하면 문의자에게 이메일이 발송됩니다.",
            f"바로가기: {inquiry_url}",
        ]
    )
    for email in _service_master_emails():
        store.enqueue_mail(
            "contact_inquiry_created",
            email,
            subject,
            body_text,
            render_branded_email(
                eyebrow="서비스 문의 접수",
                title="서비스 문의가 접수되었습니다",
                preheader=inquiry.title,
                body=[
                    "서비스 문의가 접수되었습니다.",
                    "서비스 관리자 페이지에서 답변을 등록하면 문의자에게 이메일이 발송됩니다.",
                ],
                meta=[
                    ("문의 ID", inquiry.contact_inquiry_id),
                    ("제목", inquiry.title),
                    ("이름", inquiry.sender_name),
                    ("이메일", str(inquiry.sender_email)),
                    ("접수 시각", received_at),
                ],
                sections=[("문의 본문", inquiry.body)],
                button_label="문의 확인하기",
                button_url=inquiry_url,
            ),
        )
    return ok(request, inquiry.model_dump(mode="json"))


@router.get("/public/judge-status")
@request_database
def judge_status(request: Request):
    nodes = list(store.judge_nodes.values())
    active_since = now_utc() - timedelta(seconds=max(5, settings.judge_node_active_window_seconds))
    active_nodes = [node for node in nodes if node.schedulable and node.allow_zoj and node.last_heartbeat_at >= active_since]
    # Submission activity is private: even aggregate workload can reveal
    # competitors' submissions during a contest.
    return ok(
        request,
        {
            "active_node_count": len(active_nodes),
        },
    )


@router.get("/public/rules")
@request_database
def rules(request: Request):
    return ok(
        request,
        {
            "sections": [
                {"anchor": "login", "title": "참가자 이메일 OTP 로그인"},
                {"anchor": "submit", "title": "대회 종료 전까지 제출 가능"},
                {"anchor": "freeze", "title": "종료 1시간 전 프리즈"},
            ]
        },
    )
