from scanner.extractor import extract_article, is_html_response, parse_jsonld
from bs4 import BeautifulSoup

from .conftest import fixture_bytes


def test_good_article_metadata_and_body():
    a = extract_article(fixture_bytes("article_good.html"), "https://goodblog.com/blog/technical-seo-audit-large-sites")
    assert a.extraction_status == "ok"
    assert a.title == "How to Run a Technical SEO Audit for a 10,000-Page Site"
    assert a.canonical_url == "https://goodblog.com/blog/technical-seo-audit-large-sites"
    assert a.meta_description.startswith("A step-by-step technical SEO audit")
    assert a.author == "Priya Raman"
    assert "blogposting" in a.jsonld_types
    heads = [h for _, h in a.headings]
    assert "Step 2: Compare the crawl with server log files" in heads
    assert a.word_count > 500
    assert a.code_block_count == 1 and a.ordered_list_count == 1


def test_boilerplate_removed():
    a = extract_article(fixture_bytes("article_good.html"), "https://goodblog.com/blog/technical-seo-audit-large-sites")
    body = a.body_text.lower()
    for junk in ("accept all cookies", "join our newsletter", "all rights reserved", "pricing",
                 "seo myths you should stop believing", "tweet"):
        assert junk not in body, junk


def test_outbound_links_exclude_internal_and_share_links():
    a = extract_article(fixture_bytes("article_good.html"), "https://goodblog.com/blog/technical-seo-audit-large-sites")
    domains = {link.split("/")[2] for link in a.outbound_links}
    assert domains == {"developers.google.com", "www.botify.com"}
    assert not any("twitter.com" in link or "facebook.com" in link for link in a.outbound_links)


def test_dates_collected_from_multiple_sources():
    a = extract_article(fixture_bytes("article_good.html"), "https://goodblog.com/blog/technical-seo-audit-large-sites")
    sources = {(e.source, e.kind) for e in a.date_evidence}
    assert ("jsonld_datePublished", "published") in sources
    assert ("jsonld_dateModified", "modified") in sources
    assert ("meta_article_published_time", "published") in sources
    assert ("meta_article_modified_time", "modified") in sources
    assert ("visible_text", "published") in sources


def test_js_rendered_page_detected_not_rendered():
    a = extract_article(fixture_bytes("article_js.html"), "https://spa.com/post/some-article")
    assert a.extraction_status == "js_rendered_suspected"
    assert a.word_count < 20


def test_malformed_jsonld_and_html_do_not_crash():
    html = """<html><head><script type="application/ld+json">{"@type":"Article","datePublished":"2026-10-09",}</script>
    <script type="application/ld+json">{not json at all</script></head><body><p>Short</body>"""
    soup = BeautifulSoup(html, "lxml")
    objs = parse_jsonld(soup)
    assert objs and objs[0]["datePublished"] == "2026-10-09"  # trailing comma repaired
    a = extract_article(html, "https://x.com/post-a")
    assert a.extraction_status in ("thin_content", "ok")
    assert extract_article(b"", "https://x.com/post-a").extraction_status == "failed"


def test_url_date_and_modified_only_page():
    a = extract_article(fixture_bytes("article_modified_only.html"), "https://goodblog.com/2026/10/10/content-checklist-saas")
    kinds = {(e.source, e.kind) for e in a.date_evidence}
    assert ("meta_article_modified_time", "modified") in kinds
    assert ("url_path", "published") in kinds
    assert not any(e.kind == "published" and e.source.startswith("meta") for e in a.date_evidence)


def test_is_html_response():
    assert is_html_response("text/html; charset=utf-8", b"")
    assert is_html_response("", b"<!DOCTYPE html><html>")
    assert not is_html_response("application/pdf", b"%PDF-1.7")


def test_bot_challenge_page_reported_not_scored():
    html = """<html><head><title>Just a moment...</title></head><body><h1>Checking your browser before accessing
    govtech.com</h1><p>This process is automatic.</p></body></html>"""
    a = extract_article(html, "https://www.govtech.com/security/shadow-ai")
    assert a.extraction_status == "blocked_bot_protection"
    assert "not bypassed" in " ".join(a.extraction_notes)


def test_jsonld_article_body_used_when_html_is_thin():
    body = " ".join(f"Sentence number {i} explains a concrete step of the shadow AI policy rollout." for i in range(40))
    html = ('<html><head><script type="application/ld+json">{"@type":"NewsArticle","headline":"Shadow AI",'
            '"datePublished":"2026-10-06T03:59:00Z","articleBody":"' + body + '"}</script></head>'
            '<body><article><h1>Shadow AI</h1><p>Short teaser only.</p></article></body></html>')
    a = extract_article(html, "https://www.govtech.com/security/shadow-ai")
    assert a.word_count > 300 and a.extraction_status == "ok"
    assert len(a.paragraphs) >= 5
    assert any("JSON-LD articleBody" in n for n in a.extraction_notes)


def test_thin_container_falls_back_to_body():
    paras = "".join(f"<p>Paragraph {i} has enough words to count as real article content for the test.</p>" for i in range(20))
    html = f"<html><body><main><p>Teaser.</p></main><div class='story'>{paras}</div></body></html>"
    a = extract_article(html, "https://x.com/blog/long-post")
    assert a.word_count > 200
