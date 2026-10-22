"""Search metadata derived only from the anonymous, public website surface.

HTML escaping belongs to the document renderer. This module returns plain text
and structured data so the same metadata can be used by the HTML and SPA views.
"""

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from app.services.store import store
from app.settings import settings


INDEX_ROBOTS = "index, follow, max-image-preview:large"
NOINDEX_ROBOTS = "noindex, nofollow"
CORE_PAGES = {
    "/": {
        "title": "홈 | ZOJ",
        "heading": "ZOJ · Zerone Online Judge",
        "description": "ZOJ는 프로그래밍 대회의 문제, 참가자, 코드 제출, 자동 채점과 스코어보드를 관리하는 플랫폼입니다.",
        "paragraphs": [
            "ZOJ는 문제 등록, 참가자 관리, 코드 제출과 자동 채점, 스코어보드와 결과 공개를 지원합니다.",
            "진행 중인 대회와 예정된 대회를 살펴보고, 공지사항과 이용안내를 확인하세요.",
        ],
    },
    "/about": {
        "title": "ZOJ 소개 | ZOJ",
        "heading": "ZOJ 소개",
        "description": "ZOJ의 문제·테스트 검증, 참가자 관리, 자동 채점, 스코어보드와 결과 공개 기능을 소개합니다.",
        "paragraphs": [
            "ZOJ는 실제 프로그래밍 대회 운영 과정에서 필요한 기능을 모은 온라인 저지입니다.",
            "출제와 검수, 참가자 관리, 코드 제출과 채점, 공지와 질문 답변, 최종 결과 공개까지 하나의 흐름으로 연결합니다.",
        ],
    },
    "/editor": {
        "title": "Editor 소개 | ZOJ",
        "heading": "ZOJ Editor 소개",
        "description": "ZOJ Editor는 지문·테스트·풀이와 검수 기록을 관리하는 문제 제작 도구입니다. ZOJ 채점기 검증, 대회 문제 등록과 패키지 내보내기를 지원합니다.",
        "paragraphs": [
            "대학·동아리 대회와 교육용 대회를 준비하는 출제팀의 작업 공간입니다. 지문·테스트·풀이·채점 기준과 검수 기록을 하나의 문제로 관리합니다.",
            "Polygon에서 익숙한 문제 제작 방식을 바탕으로 ZOJ 로그인, 한국어 작업 안내, ZOJ 실제 채점 환경에서의 검수와 대회 등록을 연결합니다.",
            "출제와 검수는 Editor에서, 참가자 관리·제출·채점·스코어보드는 ZOJ에서 이어집니다. 문제 소유자는 검증된 패키지를 업로드 권한이 있는 ZOJ 대회에 등록할 수 있습니다.",
        ],
    },
    "/contests": {
        "title": "대회 목록 | ZOJ",
        "heading": "프로그래밍 대회 목록",
        "description": "ZOJ에서 열리는 프로그래밍 대회를 찾아보세요. 대회 소개와 주최, 진행 일정, 참가 현황 및 종료된 대회의 공개 자료를 확인할 수 있습니다.",
        "paragraphs": ["대회별 소개와 주최, 시작·종료 일정 및 참가 현황을 확인하세요. 종료된 대회의 자료는 대회 운영진이 설정한 공개 범위에 따라 제공됩니다."],
    },
    "/notices": {
        "title": "공지사항 | ZOJ",
        "heading": "ZOJ 공지사항",
        "description": "ZOJ의 서비스 업데이트, 점검과 긴급 공지사항을 확인합니다.",
        "paragraphs": ["서비스 업데이트, 점검과 긴급 공지사항을 확인합니다."],
    },
    "/judge-status": {
        "title": "채점 안내 | ZOJ",
        "heading": "채점 서버 상태",
        "description": "ZOJ 채점 서버의 연결 상태를 확인하세요. 서버 연결 정보가 주기적으로 갱신되며, 코드 제출부터 결과 확인까지의 채점 과정도 안내합니다.",
        "paragraphs": ["채점 서버의 연결 상태를 주기적으로 확인합니다. 내 코드의 진행 상황과 결과는 대회 채점현황에서 확인할 수 있습니다."],
    },
    "/support": {
        "title": "지원 안내 | ZOJ",
        "heading": "ZOJ 지원 안내",
        "description": "ZOJ 이용안내, 대회 참가 규정, 로그인과 코드 제출에 관한 도움말을 확인하세요. 개인정보 처리 안내와 서비스 문의도 지원 안내에서 찾을 수 있습니다.",
        "paragraphs": [
            "등록된 이메일로 로그인하고 내 대회에서 참가할 대회를 선택할 수 있습니다.",
            "문제 열람, 코드 제출, 채점 결과와 스코어보드 확인 방법, 대회 규정과 개인정보 처리 안내를 제공합니다. 서비스 문의도 이곳에서 접수할 수 있습니다.",
        ],
    },
    "/support/rules": {
        "title": "대회 참가 규정 | ZOJ",
        "heading": "대회 참가 규정",
        "description": "ZOJ 대회 참가 유형과 접근 범위, 채점 결과와 스코어보드, 프리즈 및 종료 후 자료 공개에 관한 서비스 기준을 확인하세요.",
        "paragraphs": [
            "참가팀은 하나의 참가 유형에 속하며, 유형별 문제와 스코어보드는 구분됩니다. 대회 시작 전과 진행 중의 자료는 참가 권한에 따라 열립니다.",
            "프리즈가 적용되면 공개 스코어보드의 결과 반영이 제한됩니다. 최종 순위 공개와 종료 후 문제·제출·스코어보드·게시판·해설의 공개 범위는 대회 운영진이 관리합니다.",
        ],
    },
    "/support/help": {
        "title": "자주 묻는 질문 | ZOJ",
        "heading": "ZOJ 도움말",
        "description": "로그인, 대회 접근, 코드 제출과 채점 결과, 스코어보드 프리즈 및 세션 만료에 관한 자주 묻는 질문과 답변을 확인하세요.",
        "paragraphs": [
            "대회에 등록된 이메일로 로그인했는지 확인하고, 문제 접근이 안 된다면 대회 일정과 공개 범위를 확인해 주세요.",
            "제출 상태는 대회 채점현황에서, 전체 서버의 연결과 대기 상황은 채점 상태 화면에서 확인할 수 있습니다. 오랫동안 변화가 없다면 대회명과 제출번호를 함께 적어 문의해 주세요.",
        ],
    },
    "/support/privacy": {
        "title": "개인정보처리방침 | ZOJ",
        "heading": "ZOJ 개인정보처리방침",
        "description": "Zerone Online Judge의 개인정보 처리 기준을 안내합니다. 수집 항목, 이용 목적, 보관과 파기 기준 및 개인정보 관련 문의 방법을 확인하세요.",
        "paragraphs": [
            "대회 참가자 등록과 인증을 위한 이름·이메일·팀명·참가 유형, 서비스 운영과 보안을 위한 로그인·제출·채점·접속 기록의 처리 기준을 안내합니다.",
            "개인정보 이용 목적과 보관·파기 기준을 확인할 수 있으며, 관련 문의는 서비스 문의 화면에서 접수할 수 있습니다.",
        ],
    },
    "/support/contact": {
        "title": "서비스 문의 | ZOJ",
        "heading": "ZOJ 서비스 문의",
        "description": "ZOJ 이용 중 궁금한 점과 불편한 점을 남겨 주세요. 서비스 문의 양식으로 접수하면 입력한 이메일 주소로 답변을 받을 수 있습니다.",
        "paragraphs": ["이용 중 불편했던 점이나 궁금한 점을 이름, 이메일, 문의 제목과 내용으로 남겨 주세요. 답변은 입력한 이메일 주소로 보내드립니다."],
    },
}

NAVIGATION_LINKS = [
    {"href": "/about", "label": "ZOJ 소개"},
    {"href": "/editor", "label": "Editor 소개"},
    {"href": "/contests", "label": "대회 목록"},
    {"href": "/notices", "label": "공지사항"},
    {"href": "/judge-status", "label": "채점 안내"},
    {"href": "/support", "label": "지원 안내"},
]


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.suppressed += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "hr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def plain_text(value: str, limit: int | None = None) -> str:
    """Remove markup from public author-supplied text, without interpreting URLs."""
    parser = _PlainTextParser()
    parser.feed(value or "")
    text = "".join(parser.parts)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+)", "", text)
    text = re.sub(r"[`*_~]+", "", text)
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def public_origin() -> str:
    """Never trust Host/Forwarded headers when producing canonical URLs."""
    parsed = urlsplit(settings.public_base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return "https://zoj.kr"
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def normalize_seo_path(value: str) -> str:
    if not value or len(value) > 8192 or not value.startswith("/"):
        raise ValueError("A local absolute page path is required.")
    if value.startswith("//") or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid page path.")
    parsed = urlsplit(value)
    path = parsed.path
    # Reject encoded separators and traversal instead of letting different URL
    # parsers disagree about which document is being described.
    for _ in range(4):
        decoded = unquote(path)
        if re.search(r"%2f|%5c", path, flags=re.IGNORECASE):
            raise ValueError("Encoded path separators are not supported.")
        if any(segment in {".", ".."} for segment in decoded.split("/")):
            raise ValueError("Path traversal is not supported.")
        if any(ord(char) < 32 for char in decoded) or any(char in decoded for char in '\\<>"'):
            raise ValueError("Invalid page path.")
        if decoded == path:
            break
        path = decoded
    if "%" in path or "//" in path:
        raise ValueError("Invalid page path.")
    path = path.rstrip("/") or "/"
    if path == "/notices":
        notice_id = parse_qs(parsed.query).get("noticeId", [""])[0]
        if notice_id and re.fullmatch(r"[A-Za-z0-9_-]+", notice_id):
            path = f"/notices/{notice_id}"
    elif path == "/support":
        tab = parse_qs(parsed.query).get("tab", [""])[0]
        if tab in {"rules", "help", "privacy", "contact"}:
            path = f"/support/{tab}"
    return quote(path, safe="/-_.~")


def _breadcrumbs(origin: str, entries: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": index + 1, "name": label, "item": origin + path}
            for index, (path, label) in enumerate([("/", "ZOJ"), *entries])
        ],
    }


def _page(path: str, title: str, heading: str, description: str, paragraphs: list[str],
          *, links: list[dict] | None = None, indexable: bool = True, status_code: int = 200,
          breadcrumbs: list[tuple[str, str]] | None = None) -> dict:
    origin = public_origin()
    canonical = origin + path
    structured_data = []
    if indexable:
        structured_data.append({
            "@context": "https://schema.org", "@type": "WebPage", "url": canonical,
            "name": title, "description": description, "inLanguage": "ko-KR",
            "isPartOf": {"@id": origin + "/#website"},
        })
        if path != "/":
            structured_data.append(_breadcrumbs(origin, breadcrumbs or [(path, heading)]))
    return {
        "path": path, "title": title, "description": description, "canonical": canonical,
        "robots": INDEX_ROBOTS if indexable else NOINDEX_ROBOTS,
        "structured_data": structured_data, "heading": heading, "paragraphs": paragraphs,
        "links": links if links is not None else [dict(link) for link in NAVIGATION_LINKS],
        "status_code": status_code,
    }


def _not_found(path: str) -> dict:
    return _page(path, "페이지를 찾을 수 없습니다 | ZOJ", "페이지를 찾을 수 없습니다",
                 "요청한 페이지가 없거나 공개되지 않았습니다.",
                 ["주소를 확인하거나 대회 목록과 공지사항에서 공개된 내용을 찾아보세요."],
                 indexable=False, status_code=404)


PRIVATE_PAGES = {
    "/login": "로그인",
    "/admin": "관리자 콘솔",
    "/admin/contests": "대회 관리",
    "/admin/notices": "서비스 공지",
    "/admin/notices/new": "새 공지 작성",
    "/admin/judge": "채점 관리",
    "/admin/analytics": "운영 통계",
    "/admin/audit-logs": "운영 로그",
    "/admin/inquiries": "문의 관리",
    "/operator": "대회 운영",
}

OPERATOR_PAGES = {
    "": "대회 운영",
    "settings": "대회 설정",
    "operators": "운영자 관리",
    "notices": "대회 공지",
    "board": "대회 게시판",
    "participants": "참가팀 관리",
    "problems": "문제 관리",
    "problem-review": "문제 모아보기",
    "submissions": "제출 목록",
    "scoreboard": "스코어보드",
    "scoreboard/presentation": "프레젠테이션 스코어보드",
    "audit-logs": "운영 로그",
    "guide": "운영 가이드",
}

CONTEST_PAGES = {
    "submissions": "채점현황",
    "scoreboard": "스코어보드",
    "board": "대회 게시판",
}


def _private_page_title(path: str) -> str | None:
    if re.fullmatch(r"/presentation/contests/[^/]+", path):
        return "프레젠테이션 스코어보드"
    if path in PRIVATE_PAGES:
        return PRIVATE_PAGES[path]
    operator = re.fullmatch(r"/operator/contests/[^/]+(?:/(.+))?", path)
    if operator:
        return OPERATOR_PAGES.get(operator.group(1) or "")
    contest = re.fullmatch(r"/contests/[^/]+/(.+)", path)
    if not contest:
        return None
    section = contest.group(1)
    if section in CONTEST_PAGES:
        return CONTEST_PAGES[section]
    if section == "problems":
        return "문제집"
    problem = re.fullmatch(r"problems/[^/]+(?:/([^/]+))?", section)
    if problem:
        return {"submit": "코드 제출", "editorial": "해설"}.get(problem.group(1), "문제 보기")
    return None


def _format_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y년 %m월 %d일 %H:%M")


def build_page_metadata(requested_path: str) -> dict:
    path = normalize_seo_path(requested_path)
    if path in CORE_PAGES:
        breadcrumbs = None
        if path.startswith("/support/"):
            breadcrumbs = [("/support", "지원 안내"), (path, CORE_PAGES[path]["heading"])]
        metadata = _page(path, **CORE_PAGES[path], breadcrumbs=breadcrumbs)
        if path == "/":
            origin = public_origin()
            metadata["structured_data"].extend([
                {"@context": "https://schema.org", "@type": "WebSite", "@id": origin + "/#website",
                 "url": origin + "/", "name": "ZOJ", "alternateName": "Zerone Online Judge",
                 "inLanguage": "ko-KR", "publisher": {"@id": origin + "/#organization"}},
                {"@context": "https://schema.org", "@type": "Organization", "@id": origin + "/#organization",
                 "name": "ZOJ", "alternateName": "Zerone Online Judge", "url": origin + "/"},
            ])
        elif path == "/contests":
            metadata["links"] = [
                {"href": f"/contests/{quote(contest.contest_id, safe='')}", "label": plain_text(contest.title, 160)}
                for contest in store.visible_public_contests()
            ] + metadata["links"]
        elif path == "/notices":
            metadata["links"] = [
                {"href": f"/notices/{quote(notice.service_notice_id, safe='')}", "label": plain_text(notice.title, 160)}
                for notice in sorted(store.service_notices.values(), key=lambda item: item.published_at, reverse=True)
            ] + metadata["links"]
        if path == "/support" or path.startswith("/support/"):
            metadata["links"] = [
                {"href": support_path, "label": support_page["heading"]}
                for support_path, support_page in CORE_PAGES.items()
                if support_path == "/support" or support_path.startswith("/support/")
            ] + metadata["links"]
        return metadata

    contest_match = re.fullmatch(r"/contests/([^/]+)", path)
    if contest_match:
        contest = store.get_public_contest(contest_match.group(1))
        if not contest:
            return _not_found(path)
        title = plain_text(contest.title, 120) or "프로그래밍 대회"
        organizer = plain_text(contest.organization_name, 120)
        overview = plain_text(contest.overview, 1200)
        description = plain_text(f"{title}. {organizer} 주최. {overview}", 160)
        paragraphs = [overview] if overview else []
        if organizer:
            paragraphs.append(f"주최: {organizer}")
        paragraphs.append(f"대회 일정: {_format_time(contest.start_at)} ~ {_format_time(contest.end_at)} (한국 표준시)")
        return _page(path, title + " | ZOJ", title, description, paragraphs,
                     breadcrumbs=[("/contests", "대회 목록"), (path, title)])

    notice_match = re.fullmatch(r"/notices/([^/]+)", path)
    if notice_match:
        notice = store.service_notices.get(notice_match.group(1))
        if not notice:
            return _not_found(path)
        title = plain_text(notice.title, 120) or "공지사항"
        summary = plain_text(notice.summary, 160)
        body = plain_text(notice.body, 12000)
        metadata = _page(path, title + " | ZOJ", title,
                         summary or plain_text(notice.body, 160) or "ZOJ 서비스 공지사항입니다.",
                         [text for text in [summary, body] if text],
                         breadcrumbs=[("/notices", "공지사항"), (path, title)])
        metadata["structured_data"][0]["datePublished"] = notice.published_at.isoformat()
        return metadata

    private_title = _private_page_title(path)
    if private_title:
        return _page(path, private_title + " | ZOJ", private_title,
                     "로그인과 접근 권한이 필요한 ZOJ 페이지입니다.",
                     ["로그인 후 계정에 허용된 대회와 관리 기능을 이용할 수 있습니다."],
                     indexable=False)
    return _not_found(path)


def build_sitemap_xml() -> str:
    origin = public_origin()
    paths = list(CORE_PAGES)
    paths.extend(f"/contests/{quote(contest.contest_id, safe='')}" for contest in store.visible_public_contests())
    paths.extend(f"/notices/{quote(notice.service_notice_id, safe='')}" for notice in store.service_notices.values())
    root = ElementTree.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for path in dict.fromkeys(paths):
        entry = ElementTree.SubElement(root, "url")
        ElementTree.SubElement(entry, "loc").text = origin + path
    # Neither contests nor service notices have a reliable last-modified field.
    # Omitting lastmod is more accurate than treating request/publication time as
    # a modification, especially after an operator edits an existing document.
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="unicode")


def build_robots_txt() -> str:
    return "\n".join([
        "User-agent: *", "Allow: /", "Disallow: /api/", "Disallow: /minio/",
        "Disallow: /minio-console/", "", f"Sitemap: {public_origin()}/sitemap.xml", "",
    ])
