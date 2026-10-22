"""Index only public overviews; never publish authenticated contest resources."""
import json
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestResourceAccess, ContestStatus, now_utc
from app.services.seo import CORE_PAGES, build_page_metadata, build_sitemap_xml, normalize_seo_path, plain_text
from app.services.store import store
from app.settings import settings


client = TestClient(app)
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@pytest.fixture(autouse=True)
def canonical_origin(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://zoj.kr/")


@pytest.fixture
def public_contest():
    now = now_utc()
    contest = store.create_contest(
        "SEO 공개 대회 " + uuid4().hex[:8], "공개 주최기관", "공개 **대회 설명**입니다.",
        start_at=now + timedelta(days=2), end_at=now + timedelta(days=2, hours=3),
        freeze_at=now + timedelta(days=2, hours=2), status=ContestStatus.OPEN,
    )
    store.update_contest_settings(
        contest.contest_id, emergency_notice="PRIVATE_EMERGENCY_NOTICE",
        notice_access_after_end=ContestResourceAccess.PRIVATE,
    )
    division = store.create_contest_division(contest.contest_id, "seo", "PRIVATE_DIVISION_NAME")
    store.create_problem(contest.contest_id, division.division_id, "A", "PRIVATE_PROBLEM_TITLE", "PRIVATE_PROBLEM_BODY", 1000, 128, {}, 1)
    store.create_participant_team(contest.contest_id, division.division_id, "PRIVATE_TEAM_NAME", "PRIVATE_PERSON_NAME", f"seo-{uuid4().hex}@private.example", [])
    return contest


def sitemap_locations():
    response = client.get("/api/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(response.text)
    return [element.text for element in root.findall("s:url/s:loc", SITEMAP_NS)]


@pytest.mark.parametrize("path", list(CORE_PAGES))
def test_public_pages_have_unique_canonical_metadata_and_structured_data(path):
    response = client.get("/api/public/seo", params={"path": path})
    assert response.status_code == 200
    metadata = response.json()["data"]
    assert metadata["path"] == path
    assert metadata["canonical"] == "https://zoj.kr" + path
    assert metadata["title"] and metadata["description"] and metadata["heading"]
    assert metadata["paragraphs"]
    assert metadata["robots"].startswith("index, follow")
    assert metadata["status_code"] == 200
    assert metadata["structured_data"][0]["@type"] == "WebPage"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    types = {item["@type"] for item in metadata["structured_data"]}
    if path == "/":
        assert {"WebSite", "Organization"} <= types
    else:
        assert "BreadcrumbList" in types


@pytest.mark.parametrize("tab", ["rules", "help", "privacy", "contact"])
def test_support_tabs_have_distinct_canonicals_and_preserve_legacy_links(tab):
    canonical = build_page_metadata(f"/support/{tab}")
    legacy = build_page_metadata(f"/support/?tab={tab}&utm_source=test#support-content")
    assert canonical == legacy
    assert canonical["canonical"] == f"https://zoj.kr/support/{tab}"
    assert f"https://zoj.kr/support/{tab}" in sitemap_locations()


def test_query_parameters_and_fragments_never_pollute_canonical():
    metadata = build_page_metadata("/about/?utm_source=test&access_token=secret#details")
    assert metadata["path"] == "/about"
    assert metadata["canonical"] == "https://zoj.kr/about"
    assert "secret" not in json.dumps(metadata)
    assert normalize_seo_path("/support?tab=unknown") == "/support"


@pytest.mark.parametrize("path", [
    "https://attacker.example/", "//attacker.example/path", "relative/path",
    "/../operator", "/contests/./id", "/%2e%2e/operator", "/%252e%252e/operator",
    "/contests%2fid", "/contests%252fid", "/contests\\id", "/bad%00path",
    "/bad%2500path", "/<script>", "/a//b", "/bad\npath",
])
def test_unsafe_path_is_rejected(path):
    with pytest.raises(ValueError):
        normalize_seo_path(path)
    response = client.get("/api/public/seo", params={"path": path})
    assert response.status_code == 400
    assert "attacker" not in response.text


def test_canonical_does_not_use_untrusted_request_headers():
    response = client.get("/api/public/seo", params={"path": "/about"}, headers={
        "Host": "attacker.example", "X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "http",
    })
    assert response.json()["data"]["canonical"] == "https://zoj.kr/about"


def test_public_contest_metadata_exposes_only_public_overview(public_contest):
    metadata = build_page_metadata(f"/contests/{public_contest.contest_id}")
    encoded = json.dumps(metadata, ensure_ascii=False)
    assert public_contest.title in metadata["title"]
    assert "공개 주최기관" in encoded
    assert "공개 대회 설명입니다." in encoded
    assert "한국 표준시" in encoded
    assert "PRIVATE_" not in encoded
    assert "@private.example" not in encoded
    assert "emergency_notice" not in encoded
    assert "Event" not in {item["@type"] for item in metadata["structured_data"]}
    assert f"https://zoj.kr/contests/{public_contest.contest_id}" in sitemap_locations()
    listing = build_page_metadata("/contests")
    assert {"href": f"/contests/{public_contest.contest_id}", "label": public_contest.title} in listing["links"]


@pytest.mark.parametrize("status", [ContestStatus.DRAFT, ContestStatus.SCHEDULE_TBD, ContestStatus.SCHEDULED])
def test_unpublished_contest_has_no_metadata_or_sitemap_entry(public_contest, status):
    store.update_contest_settings(public_contest.contest_id, status=status)
    metadata = build_page_metadata(f"/contests/{public_contest.contest_id}")
    assert metadata["status_code"] == 404
    assert metadata["robots"] == "noindex, nofollow"
    assert metadata["structured_data"] == []
    assert public_contest.title not in json.dumps(metadata, ensure_ascii=False)
    assert f"https://zoj.kr/contests/{public_contest.contest_id}" not in sitemap_locations()
    assert public_contest.title not in json.dumps(build_page_metadata("/contests"), ensure_ascii=False)


def test_metadata_and_sitemap_follow_publish_unpublish_and_notice_edits(public_contest):
    contest_path = f"/contests/{public_contest.contest_id}"
    assert build_page_metadata(contest_path)["status_code"] == 200
    store.update_contest_settings(public_contest.contest_id, status=ContestStatus.DRAFT)
    assert build_page_metadata(contest_path)["status_code"] == 404
    assert "https://zoj.kr" + contest_path not in sitemap_locations()
    store.update_contest_settings(public_contest.contest_id, status=ContestStatus.OPEN)
    assert build_page_metadata(contest_path)["status_code"] == 200
    assert "https://zoj.kr" + contest_path in sitemap_locations()
    notice = store.create_service_notice("SEO 공지", "공개 요약", "공지 본문입니다.")
    notice_path = f"/notices/{notice.service_notice_id}"
    assert "https://zoj.kr" + notice_path in sitemap_locations()
    assert build_page_metadata(notice_path)["title"] == "SEO 공지 | ZOJ"
    store.update_service_notice(notice.service_notice_id, title="수정된 SEO 공지", summary="변경된 요약")
    assert build_page_metadata(notice_path)["description"] == "변경된 요약"
    assert build_page_metadata(notice_path)["title"].startswith("수정된 SEO 공지")
    assert build_page_metadata(f"/notices?noticeId={notice.service_notice_id}&q=ignored") == build_page_metadata(notice_path)
    assert store.delete_service_notice(notice.service_notice_id)
    assert build_page_metadata(notice_path)["status_code"] == 404
    assert "https://zoj.kr" + notice_path not in sitemap_locations()


@pytest.mark.parametrize("path,expected_title", [
    ("/login", "로그인"), ("/admin", "관리자 콘솔"), ("/admin/contests", "대회 관리"),
    ("/admin/analytics", "운영 통계"), ("/admin/notices", "서비스 공지"),
    ("/admin/notices/new", "새 공지 작성"), ("/operator", "대회 운영"),
    ("/operator/contests/any-id/settings", "대회 설정"),
    ("/operator/contests/any-id/guide", "운영 가이드"),
    ("/operator/contests/any-id/scoreboard/presentation", "프레젠테이션 스코어보드"),
    ("/contests/any-id/problems", "문제집"),
    ("/contests/any-id/problems/problem-id/editorial", "해설"),
    ("/contests/any-id/problems/problem-id/submit", "코드 제출"),
    ("/contests/any-id/submissions", "채점현황"),
    ("/contests/any-id/scoreboard", "스코어보드"), ("/contests/any-id/board", "대회 게시판"),
])
def test_authenticated_and_contest_resource_pages_are_not_indexed(path, expected_title, monkeypatch):
    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("Private metadata must not look up contest/account content")
    monkeypatch.setattr(store, "get_public_contest", forbidden_lookup)
    metadata = build_page_metadata(path)
    assert metadata["title"] == expected_title + " | ZOJ"
    assert metadata["status_code"] == 200
    assert metadata["robots"] == "noindex, nofollow"
    assert metadata["structured_data"] == []
    assert metadata["canonical"] not in sitemap_locations()


@pytest.mark.parametrize("path", ["/missing", "/contests/missing", "/notices/missing", "/support/missing", "/operator/missing"])
def test_missing_pages_return_noindex_and_real_document_404(path):
    metadata = client.get("/api/public/seo", params={"path": path}).json()["data"]
    assert metadata["status_code"] == 404
    assert metadata["robots"] == "noindex, nofollow"
    assert metadata["structured_data"] == []


def test_plain_text_and_structured_metadata_do_not_include_markup(public_contest):
    payload = '<h1>대회 &amp; 안내</h1><script>alert("SECRET_SCRIPT")</script> **새 소식** [바로가기](https://example.com)'
    store.update_contest_settings(public_contest.contest_id, title=payload, overview=payload)
    metadata = build_page_metadata(f"/contests/{public_contest.contest_id}")
    assert metadata["heading"] == "대회 & 안내 새 소식 바로가기"
    assert "SECRET_SCRIPT" not in json.dumps(metadata)
    assert "<script>" not in json.dumps(metadata)
    # JSON data remains plain text; the HTML renderer escapes it when embedding.
    assert "&" in metadata["heading"]
    assert plain_text("<p>첫 문장</p><p>두 번째 문장</p>") == "첫 문장 두 번째 문장"


def test_sitemap_is_valid_xml_escapes_path_content_and_has_no_fabricated_lastmod(monkeypatch):
    monkeypatch.setattr(store, "visible_public_contests", lambda: [SimpleNamespace(contest_id='special&<>"id')])
    xml = build_sitemap_xml()
    root = ElementTree.fromstring(xml)
    locations = [element.text for element in root.findall("s:url/s:loc", SITEMAP_NS)]
    assert "https://zoj.kr/contests/special%26%3C%3E%22id" in locations
    assert len(locations) == len(set(locations))
    assert "<lastmod>" not in xml
    assert "<priority>" not in xml


def test_robots_advertises_canonical_sitemap_without_blocking_noindex_documents():
    response = client.get("/api/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://zoj.kr/sitemap.xml" in response.text
    assert "Disallow: /api/" in response.text
    assert "Disallow: /minio/" in response.text
    assert "Disallow: /operator" not in response.text
    assert "Disallow: /login" not in response.text
