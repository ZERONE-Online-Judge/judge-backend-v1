"""Bounded first-party visit collection, without raw URLs, IPs or account emails."""
import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict
from datetime import timedelta, timezone
from threading import Lock
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import SessionLocal
from app.models import now_utc
from app.orm_models import ContestRow, GeneralSessionRow, StaffAccountRow, StaffSessionRow, TeamMemberRow, TeamSessionRow, UsageEventRow
from app.services.security import decode_session_token, token_hash
from app.settings import settings

RETENTION_DAYS = 395
SERVICES = {"public": "공개 서비스", "contest": "대회 참가 화면", "operator": "대회 운영 화면", "admin": "서비스 관리"}
PAGE_LABELS = {
    "public.home": "메인", "public.about": "ZOJ 소개", "public.editor": "Editor 소개", "public.contests": "대회 목록",
    "public.notices": "공지사항", "public.notice": "공지 상세", "public.judge-status": "채점 상태",
    "public.support": "이용안내", "public.rules": "규정", "public.help": "도움말",
    "public.privacy": "개인정보처리방침", "public.contact": "서비스 문의", "public.login": "로그인",
    "contest.overview": "대회 개요", "contest.problems": "문제집", "contest.problem": "문제 풀이",
    "contest.submissions": "채점현황", "contest.scoreboard": "스코어보드", "contest.board": "질문·공지",
    "operator.home": "운영 홈", "operator.settings": "대회 설정", "operator.operators": "운영자 관리",
    "operator.notices": "공지 관리", "operator.board": "게시판 관리", "operator.participants": "참가자 관리",
    "operator.problems": "문제 관리", "operator.problem-review": "문제 모아보기", "operator.submissions": "운영자 제출",
    "operator.scoreboard": "스코어보드 운영", "operator.presentation": "순위 발표 화면", "operator.audit-logs": "대회 운영 로그",
    "admin.home": "관리 홈", "admin.contests": "대회 관리", "admin.judge": "채점 관리",
    "admin.audit-logs": "서비스 운영 로그", "admin.inquiries": "문의 관리", "admin.analytics": "운영 통계",
}
PUBLIC_PATHS = {"/": "home", "/about": "about", "/editor": "editor", "/contests": "contests", "/notices": "notices", "/judge-status": "judge-status", "/support": "support", "/login": "login"}
PUBLIC_PATHS.update({f"/support/{key}": key for key in ["rules", "help", "privacy", "contact"]})
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
BOT_PATTERN = re.compile(r"bot\b|crawler|spider|headless|lighthouse|slurp|facebookexternalhit", re.I)


def private_key(kind: str, value: str) -> str:
    return hmac.new(settings.auth_token_secret.encode(), f"usage:{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def classify_path(path: str):
    # Only predefined route categories survive; query strings, fragments, problem
    # ids and unknown paths (which might contain personal information) are dropped.
    path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
    if path in PUBLIC_PATHS:
        return "public", f"public.{PUBLIC_PATHS[path]}", None
    if re.fullmatch(rf"/notices/{UUID_PATTERN}", path):
        return "public", "public.notice", None
    if path == "/operator":
        return "operator", "operator.home", None
    if path == "/admin" or path.startswith("/admin/"):
        page = "home" if path == "/admin" else path[7:]
        key = f"admin.{page}"
        return ("admin", key, None) if key in PAGE_LABELS else None
    match = re.fullmatch(rf"/(operator/)?contests/({UUID_PATTERN})(?:/(.*))?", path)
    if not match:
        return None
    service = "operator" if match[1] else "contest"
    suffix = match[3] or ("home" if service == "operator" else "overview")
    if service == "contest" and re.fullmatch(rf"problems/{UUID_PATTERN}(?:/(?:statement|submit|editorial|submissions))?", suffix):
        suffix = "problem"
    if service == "operator" and suffix == "scoreboard/presentation":
        suffix = "presentation"
    key = f"{service}.{suffix}"
    return (service, key, str(UUID(match[2]))) if key in PAGE_LABELS else None


def referrer_host(value: str | None) -> str:
    if not value:
        return "direct"
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or len(host) > 253 or parsed.username:
            return "direct"
        if host in {"zoj.kr", "www.zoj.kr", "judge.zerone01.kr", urlsplit(settings.public_base_url).hostname}:
            return "internal"
        # Only coarse external source categories are retained: never arbitrary
        # user-provided subdomains, query parameters, search terms or full URLs.
        for domain in ["google.com", "google.co.kr", "naver.com", "daum.net", "bing.com", "github.com", "youtube.com", "instagram.com", "facebook.com", "t.co"]:
            if host == domain or host.endswith("." + domain):
                return domain
        return "external"
    except ValueError:
        return "direct"


def client_family(user_agent: str):
    ua = user_agent.lower()
    device = "tablet" if "ipad" in ua or ("android" in ua and "mobile" not in ua) else "mobile" if any(s in ua for s in ["mobile", "iphone", "ipod"]) else "desktop"
    browser = next((label for marker, label in [("edg", "Edge"), ("samsungbrowser", "Samsung Internet"), ("opr/", "Opera"), ("firefox", "Firefox"), ("fxios", "Firefox"), ("chrome", "Chrome"), ("crios", "Chrome"), ("safari", "Safari")] if marker in ua), "Other")
    return device, browser


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def authenticated_audience(db, token, contest_id):
    if not token or not (claims := decode_session_token(token)):
        return "anonymous", None
    token_type = claims.get("typ")
    # All lookups are read-only: collecting analytics must not extend auth sessions.
    now, hashed = now_utc(), token_hash(token)
    email = None
    if token_type == "general_access":
        session = db.scalar(select(GeneralSessionRow).where(GeneralSessionRow.access_token_hash == hashed, GeneralSessionRow.revoked_at.is_(None), GeneralSessionRow.access_expires_at > now))
        email = session.email if session else None
    elif token_type == "staff_access":
        session = db.scalar(select(StaffSessionRow).where(StaffSessionRow.access_token_hash == hashed, StaffSessionRow.revoked_at.is_(None), StaffSessionRow.access_expires_at > now))
        account = db.get(StaffAccountRow, session.staff_account_id) if session else None
        email = account.email if account else None
    elif token_type == "participant_access":
        session = db.scalar(select(TeamSessionRow).where(TeamSessionRow.access_token_hash == hashed, TeamSessionRow.revoked_at.is_(None), TeamSessionRow.expires_at > now))
        member = db.get(TeamMemberRow, session.team_member_id) if session else None
        if member:
            return "participant", private_key("account", member.email.lower())
    if not email:
        return "anonymous", None
    account_key = private_key("account", email.lower())
    staff = db.scalar(select(StaffAccountRow).where(StaffAccountRow.email == email))
    if staff:
        if staff.is_service_master:
            return "service_master", account_key
        scopes = json.loads(staff.contest_scopes or "{}")
        if any(scopes.values()):
            return "operator", account_key
    if db.scalar(select(TeamMemberRow.team_member_id).where(TeamMemberRow.email == email).limit(1)):
        return "participant", account_key
    return "member", account_key


_limits = OrderedDict()
_limit_lock = Lock()


def allow_event(visitor_id: str, network: str | None = None) -> bool:
    # Bound memory and per-browser write pressure even with repeated heartbeats.
    bucket = int(time.monotonic() // 60)
    keys = [(private_key("rate", visitor_id), 60)]
    if network:
        keys.append((private_key("rate-network", network), 3000))
    with _limit_lock:
        allowed = True
        for key, maximum in keys:
            old_bucket, count = _limits.pop(key, (bucket, 0))
            count = count + 1 if old_bucket == bucket else 1
            _limits[key] = (bucket, count)
            allowed = allowed and count <= maximum
        while len(_limits) > 10000:
            _limits.popitem(last=False)
        return allowed



def record_usage(payload, user_agent: str, token: str | None, network: str | None = None) -> bool:
    if not settings.feature_usage_analytics or BOT_PATTERN.search(user_agent) or not allow_event(str(payload.visitor_id), network):
        return False
    route = classify_path(payload.path)
    if not route:
        return False
    service, page_key, contest_id = route
    visitor = private_key("visitor", str(payload.visitor_id))
    event_id = private_key("event", f"{payload.visitor_id}:{payload.event_id}")
    visit = private_key("visit", f"{payload.visitor_id}:{payload.visit_id}")
    now = now_utc()
    with SessionLocal() as db:
        existing = db.get(UsageEventRow, event_id)
        if existing:
            if (existing.visitor_key, existing.visit_key, existing.page_key, existing.contest_id) != (visitor, visit, page_key, contest_id):
                return False
            if _aware(existing.created_at) < now - timedelta(hours=24):
                return False
            seconds = min(payload.active_seconds, max(0, int((now - _aware(existing.created_at)).total_seconds())) + 2)
            db.execute(update(UsageEventRow).where(UsageEventRow.event_id == event_id).values(
                active_seconds=case((UsageEventRow.active_seconds < seconds, seconds), else_=UsageEventRow.active_seconds), last_seen_at=now))
            db.commit()
            return True
        if contest_id and not db.get(ContestRow, contest_id):
            return False
        audience, account_key = authenticated_audience(db, token, contest_id)
        # Do not count redirects to inaccessible administrator/operator routes as visits.
        if service == "admin" and audience != "service_master":
            return False
        if service == "operator" and audience not in {"operator", "service_master"}:
            return False
        device, browser = client_family(user_agent)
        insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
        db.execute(insert(UsageEventRow).values(event_id=event_id, visitor_key=visitor, visit_key=visit, account_key=account_key,
            contest_id=contest_id, service=service, page_key=page_key, audience=audience, device=device, browser=browser,
            referrer_host=referrer_host(payload.referrer), active_seconds=0, created_at=now, last_seen_at=now
        ).on_conflict_do_nothing(index_elements=["event_id"]))
        db.commit()
        return True


def purge_usage_events() -> int:
    with SessionLocal() as db:
        expired = select(UsageEventRow.event_id).where(UsageEventRow.created_at < now_utc() - timedelta(days=RETENTION_DAYS)).limit(10000)
        result = db.execute(delete(UsageEventRow).where(UsageEventRow.event_id.in_(expired)))
        db.commit()
        return result.rowcount
