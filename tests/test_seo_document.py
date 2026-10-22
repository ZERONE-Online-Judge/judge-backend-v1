"""Initial HTML must be useful without JavaScript and safe for public crawling."""
import copy
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.seo_documents import router
from app.services import seo
from app.services.seo_document import render_document
from app.settings import settings


TEMPLATE = """<!doctype html><html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OLD TITLE</title><meta name="description" content="OLD DESCRIPTION">
<meta name="robots" content="index,follow"><link rel="canonical" href="https://old.test/">
<meta property="og:title" content="OLD OG"><meta name="twitter:title" content="OLD TWITTER">
<meta name="google-site-verification" content="google-from-template">
<meta name="naver-site-verification" content="naver-from-template">
<meta name="theme-color" content="#312e81"><link rel="icon" href="/favicon.png">
<link rel="modulepreload" href="/assets/vendor-hash.js">
<link rel="stylesheet" href="/assets/index-hash.css">
<script type="application/ld+json">{"old":"structured data"}</script>
<script type="application/json" id="zoj-seo-data">{"old":"metadata"}</script>
<script type="module" crossorigin src="/assets/index-hash.js"></script>
</head><body><div id="root"></div></body></html>"""


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        self.text.append(data)

    def select(self, tag, **attributes):
        return [attrs for element, attrs in self.elements if element == tag
                and all(attrs.get(key) == value for key, value in attributes.items())]


def script_data(document, script_id):
    match = re.search(r'<script\b[^>]*\bid="' + re.escape(script_id) + r'"[^>]*>(.*?)</script>', document, re.S)
    assert match
    return json.loads(match.group(1))


@pytest.fixture
def html_client(tmp_path, monkeypatch):
    template = tmp_path / "index.html"
    template.write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(settings, "frontend_html_path", str(template))
    monkeypatch.setattr(settings, "public_base_url", "https://zoj.kr")
    monkeypatch.setattr(settings, "google_site_verification", "")
    monkeypatch.setattr(settings, "naver_site_verification", "")
    public_store = SimpleNamespace(
        get_public_contest=lambda contest_id: None,
        visible_public_contests=lambda: [],
        service_notices={},
    )
    monkeypatch.setattr(seo, "store", public_store)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield client, template, public_store


def request_page(fixture, path, **kwargs):
    client, _, _ = fixture
    return client.get("/api/public/seo/document", headers={"X-Zoj-Page-Path": path}, **kwargs)


def test_public_initial_html_has_unique_metadata_visible_content_and_working_assets(html_client):
    titles, descriptions, canonicals = set(), set(), set()
    paths = ("/", "/about", "/editor", "/contests", "/notices", "/judge-status", "/support/help")
    for path in paths:
        response = request_page(html_client, path)
        metadata = seo.build_page_metadata(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-robots-tag"] == seo.INDEX_ROBOTS
        html = response.text
        parsed = Document(html)
        assert len(parsed.select("title")) == 1
        assert parsed.select("meta", name="description") == [{"name": "description", "content": metadata["description"]}]
        assert parsed.select("link", rel="canonical") == [{"rel": "canonical", "href": "https://zoj.kr" + path}]
        assert parsed.select("meta", property="og:title")[0]["content"] == metadata["title"]
        assert parsed.select("meta", property="og:url")[0]["content"] == metadata["canonical"]
        assert parsed.select("meta", name="twitter:card")[0]["content"] == "summary_large_image"
        assert parsed.select("script", src="/assets/index-hash.js")
        assert parsed.select("link", href="/assets/vendor-hash.js")
        assert parsed.select("link", href="/assets/index-hash.css")
        assert parsed.select("link", href="/favicon.png")
        assert parsed.select("meta", name="theme-color")[0]["content"] == "#312e81"
        summary = re.search(r'<main id="zoj-public-summary".*?</main>', html, re.S).group(0)
        assert "hidden" not in summary
        assert "display:none" not in summary
        assert metadata["heading"] in unescape(summary)
        assert all(paragraph in unescape(summary) for paragraph in metadata["paragraphs"])
        assert Document(summary).select("a", href="/contests")
        assert script_data(html, "zoj-seo-data") == metadata
        assert script_data(html, "zoj-structured-data") == metadata["structured_data"]
        assert "OLD TITLE" not in html and "OLD DESCRIPTION" not in html
        assert '"old":"metadata"' not in html and '"old":"structured data"' not in html
        titles.add(metadata["title"])
        descriptions.add(metadata["description"])
        canonicals.add(metadata["canonical"])
    assert len(titles) == len(descriptions) == len(canonicals) == len(paths)


def test_template_verification_is_preserved_and_environment_can_override_it(html_client, monkeypatch):
    first = Document(request_page(html_client, "/about").text)
    for provider in ("google", "naver"):
        assert first.select("meta", name=f"{provider}-site-verification") == [{
            "name": f"{provider}-site-verification", "content": f"{provider}-from-template",
        }]
    monkeypatch.setattr(settings, "google_site_verification", 'google-new"<&token')
    monkeypatch.setattr(settings, "naver_site_verification", 'naver-new"<&token')
    updated = Document(request_page(html_client, "/about").text)
    for provider in ("google", "naver"):
        assert updated.select("meta", name=f"{provider}-site-verification") == [{
            "name": f"{provider}-site-verification", "content": f'{provider}-new"<&token',
        }]


def test_author_text_cannot_break_out_of_html_or_inline_json():
    metadata = copy.deepcopy(seo.build_page_metadata("/about"))
    payload = '</script><script src="https://evil.test/x.js">alert(1)</script><img src=x onerror="bad()">&\u2028\u2029'
    for key in ("title", "description", "heading"):
        metadata[key] = payload
    metadata["paragraphs"] = [payload]
    metadata["structured_data"][0]["description"] = payload
    metadata["links"] = [
        {"href": '/contests/public?value=" onclick="bad()', "label": payload},
        {"href": "javascript:bad()", "label": "bad scheme"},
        {"href": "//evil.test", "label": "bad origin"},
    ]
    html = render_document(TEMPLATE, metadata)
    parsed = Document(html)
    assert not parsed.select("img")
    assert not parsed.select("script", src="https://evil.test/x.js")
    assert all("onerror" not in attrs and "onclick" not in attrs for _, attrs in parsed.elements)
    assert not parsed.select("a", href="javascript:bad()")
    assert not parsed.select("a", href="//evil.test")
    assert parsed.select("meta", name="description")[0]["content"] == payload
    assert script_data(html, "zoj-seo-data")["title"] == payload
    assert script_data(html, "zoj-structured-data")[0]["description"] == payload
    assert "\\u003c/script\\u003e" in html
    assert "\\u2028" in html and "\\u2029" in html
    assert len(parsed.select("script")) == 3  # The app bundle and two safe JSON blocks.


def test_missing_and_nonpublic_contests_have_the_same_generic_404(html_client):
    client, _, public_store = html_client
    public_store.contests = {"secret": SimpleNamespace(title="HIDDEN CONTEST SECRET", overview="SECRET OVERVIEW")}
    nonpublic = request_page(html_client, "/contests/secret")
    public_store.contests.clear()
    missing = request_page(html_client, "/contests/secret")
    authenticated = client.get("/api/public/seo/document", headers={
        "X-Zoj-Page-Path": "/contests/secret", "Authorization": "Bearer arbitrary-operator-token",
    })
    assert nonpublic.status_code == missing.status_code == authenticated.status_code == 404
    assert nonpublic.text == missing.text == authenticated.text
    assert "SECRET" not in nonpublic.text
    assert nonpublic.headers["x-robots-tag"] == seo.NOINDEX_ROBOTS
    assert not Document(nonpublic.text).select("script", type="application/ld+json")


@pytest.mark.parametrize("path", ["/login", "/admin", "/admin/analytics", "/admin/notices", "/admin/notices/new", "/operator/contests/secret/settings", "/contests/secret/submissions"])
def test_access_controlled_app_routes_receive_generic_noindex_documents(html_client, path):
    response = request_page(html_client, path)
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == seo.NOINDEX_ROBOTS
    metadata = script_data(response.text, "zoj-seo-data")
    assert metadata["structured_data"] == []
    assert metadata["robots"] == seo.NOINDEX_ROBOTS
    assert "로그인" in metadata["description"]


@pytest.mark.parametrize("path,canonical", [
    ("/support?tab=privacy", "https://zoj.kr/support/privacy"),
    ("/about/", "https://zoj.kr/about"),
])
def test_legacy_support_and_trailing_slash_redirect_to_canonical(html_client, path, canonical):
    response = request_page(html_client, path, follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == canonical


@pytest.mark.parametrize("path", ["//evil.test/", "https://evil.test/", "/contests/%2Fsecret"])
def test_invalid_page_header_is_a_noindex_400_not_a_redirect(html_client, path):
    response = request_page(html_client, path, follow_redirects=False)
    assert response.status_code == 400
    assert response.headers["x-robots-tag"] == seo.NOINDEX_ROBOTS
    assert "location" not in response.headers
    assert "evil.test" not in response.text


@pytest.mark.parametrize("template_body", [None, "<html><head></head><body>No React root</body></html>"])
def test_unavailable_template_returns_retryable_503_instead_of_soft_404(html_client, template_body):
    _, template, _ = html_client
    if template_body is None:
        template.unlink()
    else:
        template.write_text(template_body, encoding="utf-8")
    response = request_page(html_client, "/about")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert "잠시 후" in response.text


def test_template_is_reloaded_after_same_size_file_changes(html_client):
    _, template, _ = html_client
    first = request_page(html_client, "/about")
    assert "/assets/index-hash.js" in first.text
    previous = template.stat()
    template.write_text(TEMPLATE.replace("index-hash.js", "index-next.js"), encoding="utf-8")
    assert template.stat().st_size == previous.st_size
    os.utime(template, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
    second = request_page(html_client, "/about")
    assert "/assets/index-next.js" in second.text
    assert "/assets/index-hash.js" not in second.text


def test_head_returns_the_same_page_status_without_a_body(html_client):
    client, _, _ = html_client
    response = client.head("/api/public/seo/document", headers={"X-Zoj-Page-Path": "/about"})
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-robots-tag"] == seo.INDEX_ROBOTS
