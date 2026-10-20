"""Branded mail must preserve user content and remain useful without HTML."""
import asyncio
import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.services.mail_templates import (
    contest_reminder_mail,
    login_verification_mail,
    operator_assignment_mail,
    participant_invite_mail,
    render_branded_email,
)


class ParsedMail(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.hidden_text = []
        self._stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        style = attrs.get("style", "").replace(" ", "").lower()
        hidden = bool(self._stack and self._stack[-1][1]) or "display:none" in style or "hidden" in attrs
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}:
            self._stack.append((tag, hidden))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        self.text.append(data)
        if self._stack and self._stack[-1][1]:
            self.hidden_text.append(data)


def test_branded_mail_escapes_user_content_and_button_attributes():
    unsafe = '<script>alert("메일")</script> & <img src=x onerror=alert(1)>'
    url = 'https://zoj.kr/contests/example?name="특별"&noticeId=123'
    html = render_branded_email(
        title=f"제목 {unsafe}",
        preheader=f"미리보기 {unsafe}",
        body=[f"본문 {unsafe}"],
        meta=[(f"항목 {unsafe}", f"값 {unsafe}")],
        sections=[(f"내용 {unsafe}", f"상세 {unsafe}")],
        button_label=f"열기 {unsafe}",
        button_url=url,
    )
    parsed = ParsedMail(html)

    assert not any(tag == "script" for tag, _ in parsed.elements)
    assert not any(key.startswith("on") for _, attrs in parsed.elements for key in attrs)
    assert not any(attrs.get("src") == "x" for _, attrs in parsed.elements)
    assert any(tag == "a" and attrs.get("href") == url for tag, attrs in parsed.elements)
    rendered_text = "".join(parsed.text)
    for label in ("제목", "미리보기", "본문", "항목", "값", "내용", "상세", "열기"):
        assert f"{label} {unsafe}" in rendered_text


def test_invite_plain_text_retains_contest_team_division_and_destination():
    url = "https://zoj.kr/contests/spring"
    content = participant_invite_mail(
        contest_title="봄 대회",
        organization_name="제로원",
        team_name="함께 풀어요",
        division_name="일반부",
        contest_url=url,
    )

    for value in ("봄 대회", "함께 풀어요", "일반부", url):
        assert value in content.body_text
    assert "<table" not in content.body_text
    assert any(
        tag == "a" and attrs.get("href") == url
        for tag, attrs in ParsedMail(content.body_html).elements
    )


def test_reminder_uses_korean_local_time_in_both_alternatives():
    content = contest_reminder_mail(
        contest_title="저녁 대회",
        organization_name="제로원",
        team_name="도전",
        division_name="일반부",
        starts_at=datetime(2026, 9, 23, 11, 30, tzinfo=timezone.utc),
        remaining_label="10분",
        contest_url="https://zoj.kr/contests/evening",
    )

    expected_time = "2026년 9월 23일 20:30 KST"
    assert expected_time in content.body_text
    assert expected_time in "".join(ParsedMail(content.body_html).text)
    assert "10분" in content.subject
    assert "10분" in content.body_text


@pytest.mark.parametrize("audience", ["general", "staff", "participant"])
def test_login_code_is_copyable_but_not_in_subject_or_preview(audience):
    code = "012345"
    content = login_verification_mail(code=code, ttl_seconds=300, audience=audience)
    parsed = ParsedMail(content.body_html)

    assert code in content.body_text
    assert any(code in text for text in parsed.text)
    assert "5분" in content.body_text
    assert "5분" in "".join(parsed.text)
    assert code not in content.subject
    assert parsed.hidden_text
    assert code not in "".join(parsed.hidden_text)


def test_multiline_content_retains_line_breaks_without_css_support():
    html = render_branded_email(
        title="답변 안내",
        preheader="새 답변을 확인하세요",
        body=["첫 안내\n두 번째 안내"],
        sections=[("답변 본문", "첫 줄\n\n세 번째 줄 <사용자 입력>")],
    )

    assert "첫 안내<br" in html
    assert "첫 줄<br" in html
    assert "세 번째 줄 &lt;사용자 입력&gt;" in html
    assert sum(tag == "br" for tag, _ in ParsedMail(html).elements) >= 3


@pytest.fixture
def isolated_store(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.services.store import DbStore

    engine = create_engine(f"sqlite:///{tmp_path / 'mail.sqlite'}")
    Base.metadata.create_all(engine)
    # Use the real store methods against an isolated DB, without starting a worker.
    mail_store = DbStore.__new__(DbStore)
    mail_store._session = sessionmaker(bind=engine, expire_on_commit=False)
    yield mail_store
    engine.dispose()


@pytest.mark.parametrize("audience", ["general", "staff", "participant"])
def test_all_otp_queue_paths_use_the_dedicated_mail(audience, monkeypatch, isolated_store):
    from app.settings import settings

    monkeypatch.setattr(settings, "otp_ttl_seconds", 300)
    email = "mail-test@example.com"
    contest = isolated_store.create_contest("메일 테스트", "제로원", "테스트")
    if audience == "participant":
        division = isolated_store.create_contest_division(contest.contest_id, "A", "일반부")
        isolated_store.create_participant_team(
            contest.contest_id, division.division_id, "팀", "참가자", email, []
        )
        code = isolated_store.create_otp(contest.contest_id, email)
    else:
        isolated_store.upsert_contest_operator(contest.contest_id, email, "운영자")
        create_otp = isolated_store.create_general_otp if audience == "general" else isolated_store.create_staff_otp
        code = create_otp(email)

    item, = isolated_store.mail_queue.values()
    expected = login_verification_mail(code=code, ttl_seconds=300, audience=audience)
    assert item.mail_type == f"{audience}_otp"
    assert item.recipient_email == email
    assert item.status == "pending"
    assert item.subject == expected.subject
    assert item.body_text == expected.body_text
    assert item.body_html == expected.body_html


@pytest.mark.parametrize("starts_at", [None, datetime(2026, 9, 23, 11, 30, tzinfo=timezone.utc)])
def test_operator_assignment_retains_role_schedule_and_link(starts_at):
    url = "https://zoj.kr/operator/contests/example"
    content = operator_assignment_mail(
        contest_title="가을 대회",
        organization_name="제로원",
        display_name="손동열",
        role_label="출제자",
        starts_at=starts_at,
        console_url=url,
    )
    expected_schedule = "일정 확정 전" if starts_at is None else "2026년 9월 23일 20:30 KST"
    parsed = ParsedMail(content.body_html)
    for value in ("가을 대회", "제로원", "손동열 / 출제자", expected_schedule):
        assert value in content.body_text
        assert value in "".join(parsed.text)
    assert url in content.body_text
    assert any(tag == "a" and attrs.get("href") == url for tag, attrs in parsed.elements)


@pytest.mark.parametrize("roles, role_title, path_prefix", [
    (["master"], "마스터", "/operator/contests/"),
    (["problem_author", "settings_manager"], "출제진", "/operator/contests/"),
    (["notices_manager"], "운영진", "/operator/contests/"),
    (["problem_reviewer"], "검수진", "/operator/contests/"),
    (["participant_preview"], "운영진", "/operator/contests/"),
])
def test_operator_assignment_mail_links_to_accessible_role_route(
    roles, role_title, path_prefix, isolated_store, monkeypatch
):
    from app.routers import operator
    from app.settings import settings

    contest = isolated_store.create_contest("역할 초대", "제로원", "테스트")
    actor = isolated_store.upsert_contest_operator(contest.contest_id, "master@example.com", "마스터")
    monkeypatch.setattr(operator, "store", isolated_store)
    monkeypatch.setattr(operator, "require_contest_staff", lambda *args: actor)
    monkeypatch.setattr(settings, "public_base_url", "https://zoj.kr")
    request = SimpleNamespace(state=SimpleNamespace(request_id="mail-test"))
    payload = operator.ContestOperatorCreateRequest(
        email="invited@example.com", display_name="손동열", roles=roles
    )

    asyncio.run(operator.create_contest_operator(contest.contest_id, payload, request))

    item, = isolated_store.mail_queue.values()
    expected_path = f"{path_prefix}{contest.contest_id}"
    assert item.mail_type == "contest_operator_assigned"
    assert f"손동열 / {role_title}" in item.body_text
    destinations = [
        attrs["href"] for tag, attrs in ParsedMail(item.body_html).elements
        if tag == "a" and "href" in attrs
    ]
    destination = f"https://zoj.kr{expected_path}"
    assert destination in destinations
    assert destination in item.body_text


@pytest.mark.parametrize("operation", ["create_contest", "assign_master"])
def test_admin_assignment_mail_identifies_protected_master_and_unknown_schedule(
    operation, isolated_store, monkeypatch
):
    from app.routers import admin
    from app.settings import settings

    monkeypatch.setattr(admin, "store", isolated_store)
    monkeypatch.setattr(admin, "require_service_master", lambda *args: None)
    monkeypatch.setattr(settings, "public_base_url", "https://zoj.kr")
    request = SimpleNamespace(state=SimpleNamespace(request_id="mail-test"))
    if operation == "create_contest":
        payload = admin.ContestCreateRequest(
            title="신규 대회", organization_name="제로원", operator_email="invited@example.com"
        )
        response = asyncio.run(admin.create_contest(payload, request))
        contest_id = response["data"]["contest_id"]
    else:
        contest = isolated_store.create_contest("기존 대회", "제로원", "테스트")
        contest_id = contest.contest_id
        payload = admin.ContestOperatorCreateRequest(email="invited@example.com", display_name="손동열")
        asyncio.run(admin.create_contest_operator(contest_id, payload, request))

    item, = isolated_store.mail_queue.values()
    assert "총괄" in item.subject
    assert "일정 확정 전" in item.body_text
    assert f"https://zoj.kr/operator/contests/{contest_id}" in item.body_text
    assert "일정 확정 전" in "".join(ParsedMail(item.body_html).text)
