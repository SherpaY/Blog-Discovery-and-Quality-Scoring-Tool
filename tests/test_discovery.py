import gzip
from datetime import timedelta

from scanner.rss_discovery import discover_feeds, fetch_feed, find_feed_links_in_html, parse_feed
from scanner.search_discovery import (BingNewsRSS, GoogleNewsRSS, MediumTagFeed, decode_google_news_url,
                                      run_search_sources)
from scanner.sitemap_discovery import discover_from_sitemaps, parse_sitemap, sitemaps_from_robots
from scanner.topics import Query, expand_keywords, match_keywords, mine_title_phrases
from scanner.urlutils import domain_to_base_url, is_article_like, normalize_url
from scanner.http_client import FetchResult

from .conftest import NOW, FakeSession, fixture_bytes

WINDOW_START = NOW - timedelta(hours=168)


# ----------------------------------------------------------------------------- RSS / Atom
def test_rss2_parsing_extracts_links_titles_dates_summaries():
    res = parse_feed(fixture_bytes("rss2.xml"), "https://goodblog.com/feed")
    assert res.error is None
    assert res.is_wordpress
    assert len(res.items) == 4
    first = res.items[0]
    assert first.title == "How to Run a Technical SEO Audit for a 10,000-Page Site"
    assert first.summary == "A step-by-step technical SEO audit process."  # HTML stripped
    pub = [e for e in first.date_evidence if e.kind == "published"]
    assert pub and pub[0].value.isoformat() == "2026-10-09T08:15:00+00:00"
    assert pub[0].source == "feed_published"
    assert res.items[3].date_evidence == []  # undated item keeps no invented date


def test_atom_parsing_keeps_published_and_updated_separate():
    res = parse_feed(fixture_bytes("atom.xml"), "https://atomblog.com/feed.atom")
    assert res.error is None
    assert res.next_url == "https://atomblog.com/feed.atom?page=2"
    entry = res.items[0]
    kinds = {e.kind: e.value.isoformat() for e in entry.date_evidence}
    assert kinds["published"] == "2026-10-10T18:30:02+00:00"
    assert kinds["modified"] == "2026-10-11T08:00:00+00:00"
    only_updated = res.items[1]
    assert [e.kind for e in only_updated.date_evidence] == ["modified"]
    assert only_updated.date_evidence[0].value.isoformat() == "2026-10-09T08:00:00+00:00"  # +02:00 -> UTC


def test_empty_and_malformed_feeds_do_not_raise():
    assert parse_feed(fixture_bytes("empty_feed.xml"), "https://empty.com/feed").items == []
    assert parse_feed(b"", "https://x.com/feed").error == "empty feed"
    bad = parse_feed(fixture_bytes("malformed_feed.xml"), "https://x.com/feed")
    assert bad.items == [] and bad.error


def test_feed_pagination_follows_next_only_while_items_recent(fetcher_factory):
    s = FakeSession()
    page1 = fixture_bytes("atom.xml")
    page2 = page1.replace(b'<link rel="next" href="https://atomblog.com/feed.atom?page=2"/>', b"") \
                 .replace(b"atom-content-marketing-entry", b"page-two-entry")
    s.add("https://atomblog.com/feed.atom", page1, content_type="application/atom+xml")
    s.add("https://atomblog.com/feed.atom?page=2", page2, content_type="application/atom+xml")
    s.add("https://atomblog.com/robots.txt", b"", status=404)
    # Page 1 contains an entry with no published date -> pagination stops (cannot prove recency).
    items, msgs = fetch_feed(fetcher_factory(s), "https://atomblog.com/feed.atom", WINDOW_START, max_pages=3)
    assert len(items) == 2
    assert "https://atomblog.com/feed.atom?page=2" not in s.calls


def test_feed_discovery_helper_finds_link_tags_and_common_paths(fetcher_factory):
    html = b"""<html><head><link rel="alternate" type="application/rss+xml" href="/feed/" title="Main">
    <link rel="alternate" type="application/rss+xml" href="/comments/feed/" title="Comments Feed"></head></html>"""
    assert find_feed_links_in_html(html.decode(), "https://goodblog.com/") == ["https://goodblog.com/feed/"]

    s = FakeSession()
    s.add("https://nolinks.com/robots.txt", b"", status=404)
    s.add("https://nolinks.com/", b"<html><head></head><body>hi</body></html>", content_type="text/html")
    s.add("https://nolinks.com/rss", fixture_bytes("rss2.xml"), content_type="application/rss+xml")
    feeds, _ = discover_feeds(fetcher_factory(s), "https://nolinks.com/")
    assert feeds == ["https://nolinks.com/rss"]


# ----------------------------------------------------------------------------- Sitemaps
def test_sitemap_index_parsing():
    res = parse_sitemap(fixture_bytes("sitemap_index.xml"))
    assert res.kind == "sitemapindex"
    assert [e.loc for e in res.entries][1] == "https://goodblog.com/post-sitemap.xml"


def test_news_sitemap_publication_date_and_title():
    res = parse_sitemap(fixture_bytes("news_sitemap.xml"))
    assert res.kind == "urlset"
    e = res.entries[0]
    assert e.news_publication_date == "2026-10-10T15:30:00-04:00"
    assert e.news_title.startswith("Google confirms")


def test_malformed_and_empty_sitemaps():
    assert parse_sitemap(fixture_bytes("malformed_sitemap.xml")).error.startswith("malformed XML")
    assert parse_sitemap(b"").error == "empty sitemap"
    assert "not a sitemap" in parse_sitemap(b"<html><body>hi</body></html>").error


def test_gzip_sitemap_and_size_limit():
    gz = gzip.compress(fixture_bytes("post_sitemap.xml"))
    assert len(parse_sitemap(gz).entries) == 6
    assert parse_sitemap(gz, max_bytes=100).error.startswith("bad gzip")


def test_sitemaps_from_robots():
    assert sitemaps_from_robots(fixture_bytes("robots.txt").decode(), "https://goodblog.com") == [
        "https://goodblog.com/sitemap_index.xml"]


def test_nested_sitemap_walk_uses_lastmod_only_as_hint(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", fixture_bytes("robots.txt"), content_type="text/plain")
    s.add("https://goodblog.com/sitemap_index.xml", fixture_bytes("sitemap_index.xml"), content_type="application/xml")
    s.add("https://goodblog.com/post-sitemap.xml", fixture_bytes("post_sitemap.xml"), content_type="application/xml")
    s.add("https://goodblog.com/news-sitemap.xml", fixture_bytes("news_sitemap.xml"), content_type="application/xml")
    items, msgs = discover_from_sitemaps(fetcher_factory(s), ["https://goodblog.com/sitemap_index.xml"], WINDOW_START)
    urls = {i.url for i in items}
    # page-sitemap (lastmod 2024) is never fetched
    assert "https://goodblog.com/page-sitemap.xml" not in s.calls
    assert "https://goodblog.com/blog/technical-seo-audit-large-sites" in urls
    assert "https://goodblog.com/blog/ancient-post-2019" not in urls
    assert "https://goodblog.com/blog/no-date-post" not in urls  # undated -> skipped
    news = next(i for i in items if "core-update" in i.url)
    assert any(e.source == "news_sitemap_publication_date" and e.kind == "published" for e in news.date_evidence)
    lastmod_item = next(i for i in items if i.url.endswith("technical-seo-basics-updated"))
    assert all(e.kind == "modified" for e in lastmod_item.date_evidence)  # lastmod never becomes "published"


def test_oversized_sitemap_is_skipped(fetcher_factory):
    s = FakeSession()
    s.add("https://big.com/robots.txt", b"", status=404)
    s.add("https://big.com/sitemap.xml", b"<urlset>" + b" " * 5000 + b"</urlset>", content_type="application/xml")
    items, msgs = discover_from_sitemaps(fetcher_factory(s), ["https://big.com/sitemap.xml"], WINDOW_START, max_bytes=1000)
    assert items == [] and "too_large" in msgs[0]


# ----------------------------------------------------------------------------- URLs
def test_url_normalization_and_dedup():
    a = normalize_url("HTTPS://GoodBlog.com:443/blog/post/?utm_source=rss&b=2&a=1#comments")
    b = normalize_url("https://goodblog.com/blog//post?a=1&b=2&fbclid=xyz")
    assert a == b == "https://goodblog.com/blog/post?a=1&b=2"
    assert normalize_url("/blog/x", base="https://goodblog.com/") == "https://goodblog.com/blog/x"
    assert normalize_url("http://site.com:8080/") == "http://site.com:8080/"
    assert domain_to_base_url("www.Example.com/blog/") == "https://www.example.com/blog/"


def test_article_like_heuristics():
    assert is_article_like("https://goodblog.com/blog/technical-seo-audit-large-sites")[0]
    for bad in ("https://goodblog.com/", "https://goodblog.com/category/seo", "https://goodblog.com/tag/seo",
                "https://goodblog.com/author/priya", "https://goodblog.com/blog/page/3", "https://goodblog.com/2026/10",
                "https://goodblog.com/login", "https://goodblog.com/blog", "https://goodblog.com/image.png",
                "https://goodblog.com/blog?s=seo"):
        assert not is_article_like(bad)[0], bad


# ----------------------------------------------------------------------------- search sources
def test_google_news_rss_parsing_and_url_decoding():
    src = GoogleNewsRSS()
    res = FetchResult(url="u", final_url="https://news.google.com/rss/search", status=200,
                      content=fixture_bytes("google_news.xml"), headers={"content-type": "application/rss+xml"})
    assert src.diagnose(res) is None
    items, err = src.parse(res, Query("technical seo", "user", "technical seo"))
    assert err is None and len(items) == 2
    assert items[0].url == "https://copyblog.com/technical-seo-audit-process?utm_source=x"
    assert items[0].title == "Technical SEO Audit for Large Sites: Our Process"  # " - Publisher" stripped
    assert items[0].date_evidence[0].source == "search_result_date"
    assert items[1].resolved is False and items[1].publisher_url == "https://thinblog.com"
    assert "when%3A7d" in src.build_url("technical seo", 168)


def test_ranks_recorded_only_for_relevance_ranked_sources(fetcher_factory):
    s = FakeSession()
    s.add("https://news.google.com/robots.txt", b"", status=404)
    s.add(GoogleNewsRSS().build_url("technical seo", 168), fixture_bytes("google_news.xml"),
          content_type="application/rss+xml")
    s.add("https://medium.com/robots.txt", b"", status=404)
    s.add(MediumTagFeed().build_url("technical seo", 168), fixture_bytes("rss2.xml"), content_type="application/rss+xml")
    items, statuses, _ = run_search_sources(fetcher_factory(s), {"google_news_rss": True, "medium_tag_feed": True},
                                            [Query("technical seo", "user", "technical seo")], 168, sleep=lambda s: None)
    google = [i for i in items if i.discovery_source == "search:google_news_rss"]
    medium = [i for i in items if i.discovery_source == "search:medium_tag_feed"]
    assert [i.search_ranks[0]["rank"] for i in google] == [1, 2]
    assert medium and all(i.search_ranks == [] for i in medium)  # newest-first feed: no position claimed


def test_google_news_opaque_url_not_decoded():
    assert decode_google_news_url("https://news.google.com/rss/articles/CBMiOpaqueTokenThatCannotBeDecodedXYZ") is None
    assert decode_google_news_url("https://example.com/x") is None


def test_bing_redirect_url_extracted():
    src = BingNewsRSS()
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Bing</title>
    <item><title>Story</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fpublisher.com%2fstory-about-seo&amp;c=1</link>
    <pubDate>Sat, 10 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    res = FetchResult(url="u", final_url="https://www.bing.com/news/search", status=200, content=body)
    items, err = src.parse(res, Query("seo", "user", "seo"))
    assert items[0].url == "https://publisher.com/story-about-seo"


def test_blocked_search_source_is_disabled_not_simulated(fetcher_factory):
    s = FakeSession()
    s.add("https://news.google.com/robots.txt", b"User-agent: *\nAllow: /\n")
    s.add(GoogleNewsRSS().build_url("technical seo", 168), fixture_bytes("malformed_feed.xml"),
          content_type="text/html")
    s.add("https://medium.com/robots.txt", b"", status=404)
    s.add(MediumTagFeed().build_url("technical seo", 168), b"Forbidden", status=403)
    items, statuses, msgs = run_search_sources(
        fetcher_factory(s), {"google_news_rss": True, "medium_tag_feed": True},
        [Query("technical seo", "user", "technical seo")], 168, sleep=lambda s: None)
    assert items == []
    by_name = {st.name: st for st in statuses}
    assert not by_name["Google News RSS"].available
    assert "CAPTCHA" in by_name["Google News RSS"].message
    assert not by_name["Medium tag feed"].available and "403" in by_name["Medium tag feed"].message


def test_search_respects_robots_disallow(fetcher_factory):
    s = FakeSession()
    s.add("https://news.google.com/robots.txt", b"User-agent: *\nDisallow: /rss/search\n")
    items, statuses, _ = run_search_sources(fetcher_factory(s), {"google_news_rss": True},
                                            [Query("seo", "user", "seo")], 168, sleep=lambda s: None)
    assert statuses[0].available is False and "robots" in statuses[0].message
    assert not any("rss/search" in c for c in s.calls)


# ----------------------------------------------------------------------------- topics
def test_keyword_expansion_labels_origins():
    qs = expand_keywords(["Technical SEO audit"], {"variation_templates": ["{kw} tips"], "use_word_combinations": True},
                         synonyms={"audit": ["review"]})
    by_text = {q.text: q for q in qs}
    assert by_text["technical seo audit"].origin == "user" and by_text["technical seo audit"].is_user_supplied
    assert by_text["technical seo review"].origin == "synonym"
    assert by_text["seo audit"].origin == "combination"
    assert by_text["technical seo audit tips"].origin == "variation"
    assert all(q.user_keyword == "technical seo audit" for q in qs)


def test_keyword_matching_and_title_mining():
    qs = [Query("technical seo", "user", "technical seo")]
    assert match_keywords("A guide to Technical SEO audits", qs)
    assert not match_keywords("Social media scheduling", qs)
    mined = mine_title_phrases(["Technical SEO checklist", "The technical SEO checklist for 2026", "SEO checklist"],
                               ["technical seo"])
    assert any(q.text == "seo checklist" and q.origin == "mined_title" for q in mined)


# ----------------------------------------------------------------------------- new sources & fallbacks
def test_gdelt_parsing_and_query_errors():
    from scanner.search_discovery import GdeltDoc

    src = GdeltDoc()
    url = src.build_url("shadow ai policy", 168)
    assert "api.gdeltproject.org" in url and "mode=ArtList" in url and "startdatetime=" in url
    assert "%22shadow%20ai%20policy%22" in url  # multi-word phrase is quoted
    body = (b'{"articles":[{"url":"https://blog.example.com/shadow-ai-policy","title":"Writing a shadow AI policy",'
            b'"seendate":"20261010T121500Z","domain":"blog.example.com","language":"English"}]}')
    res = FetchResult(url=url, final_url=url, status=200, content=body, headers={"content-type": "application/json"})
    assert src.soft_error(res) is None and src.diagnose(res) is None
    items, err = src.parse(res, Query("shadow ai policy", "user", "shadow ai policy"))
    assert err is None and items[0].url == "https://blog.example.com/shadow-ai-policy"
    assert items[0].date_evidence[0].value.isoformat() == "2026-10-10T12:15:00+00:00"
    bad = FetchResult(url=url, final_url=url, status=200, content=b"The specified phrase is too short.")
    assert src.soft_error(bad).startswith("query rejected by GDELT")


def test_tag_feed_missing_tag_is_not_fatal_and_short_tags_tried(fetcher_factory):
    from scanner.search_discovery import DevToTagFeed

    s = FakeSession()
    s.add("https://dev.to/robots.txt", b"", status=404)
    s.add(DevToTagFeed().build_url("shadow ai policy template", 168), b"not found", status=404)
    s.add(DevToTagFeed().build_url("shadow ai", 168), fixture_bytes("rss2.xml"), content_type="application/rss+xml")
    queries = [Query("shadow ai policy template", "user", "shadow ai policy template"),
               Query("shadow ai policy", "combination", "shadow ai policy template"),
               Query("shadow ai", "combination", "shadow ai policy template"),
               Query("shadow ai policy template guide", "variation", "shadow ai policy template")]
    items, statuses, msgs = run_search_sources(fetcher_factory(s), {"devto_tag_feed": True}, queries, 168,
                                               max_queries=2, sleep=lambda s: None)
    assert statuses[0].available  # a missing tag does not disable the source
    assert any("tag does not exist" in m for m in msgs)
    assert items and all(i.discovery_query == "shadow ai" for i in items)  # shortest phrase tried second


def test_aggregator_copies_detected():
    from scanner.discovery import _is_aggregator

    assert _is_aggregator("https://www.msn.com/en-us/news/technology/some-story/ar-AA1abc") == "msn.com"
    assert _is_aggregator("https://flipboard.com/@x/story") == "flipboard.com"
    assert _is_aggregator("https://www.govtech.com/security/shadow-ai-in-tennessee") is None
