"""End-to-end scan against a simulated mini-web of fixture sites (no network).

Sample articles have known publication dates and deliberately varied quality:
  good article (confirmed, strong)            goodblog.com/blog/technical-seo-audit-large-sites
  conflicting dates (date review queue)       goodblog.com/blog/content-marketing-budgets-2026
  modified-date only (date review queue)      goodblog.com/blog/content-checklist-saas
  old article updated this week (rejected)    goodblog.com/blog/technical-seo-basics-updated
  undated feed item returning 404 (failed)    goodblog.com/blog/undated-technical-seo-note
  off-topic news post (keyword filter)        goodblog.com/news/google-october-core-update
  near-copy found via Google News RSS         copyblog.com/technical-seo-audit-process
  thin article behind opaque Google link      thinblog.com/posts/ultimate-guide-technical-seo
"""

import csv

import pytest

from scanner.pipeline import ScanAlreadyRunning, ScanLock, ScanRunner, request_stop
from scanner.search_discovery import GoogleNewsRSS
from scanner.storage import Storage

from .conftest import NOW, FakeSession, fixture_bytes, public_resolver

ROBOTS = b"User-agent: *\nDisallow: /private/\n\nSitemap: https://goodblog.com/sitemap_index.xml\n"


def mini_web() -> FakeSession:
    s = FakeSession()
    html, xml, rss = "text/html; charset=utf-8", "application/xml", "application/rss+xml"
    s.add("https://goodblog.com/robots.txt", ROBOTS, content_type="text/plain")
    s.add("https://goodblog.com/", b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed/">'
                                   b"</head><body>Home</body></html>", content_type=html)
    s.add("https://goodblog.com/feed/", fixture_bytes("rss2.xml"), content_type=rss)
    s.add("https://goodblog.com/sitemap_index.xml", fixture_bytes("sitemap_index.xml"), content_type=xml)
    s.add("https://goodblog.com/post-sitemap.xml", fixture_bytes("post_sitemap.xml"), content_type=xml)
    s.add("https://goodblog.com/news-sitemap.xml", fixture_bytes("news_sitemap.xml"), content_type=xml)
    s.add("https://goodblog.com/blog/technical-seo-audit-large-sites?utm_source=rss&utm_medium=rss",
          fixture_bytes("article_good.html"), content_type=html)
    s.add("https://goodblog.com/blog/technical-seo-audit-large-sites", fixture_bytes("article_good.html"),
          content_type=html)
    s.add("https://goodblog.com/blog/content-marketing-budgets-2026", fixture_bytes("article_conflicting_dates.html"),
          content_type=html)
    s.add("https://goodblog.com/blog/content-checklist-saas", fixture_bytes("article_modified_only.html"),
          content_type=html)
    s.add("https://goodblog.com/blog/technical-seo-basics-updated", fixture_bytes("article_old_updated.html"),
          content_type=html)
    s.add("https://goodblog.com/news/google-october-core-update", fixture_bytes("article_news_short.html"),
          content_type=html)
    # Google News RSS search
    s.add("https://news.google.com/robots.txt", b"User-agent: *\nAllow: /rss/\n", content_type="text/plain")
    s.add(GoogleNewsRSS().build_url("technical seo", 168), fixture_bytes("google_news.xml"), content_type=rss)
    s.add("https://copyblog.com/robots.txt", b"", status=404)
    s.add("https://copyblog.com/technical-seo-audit-process?utm_source=x", fixture_bytes("article_duplicate.html"),
          content_type=html)
    s.add("https://thinblog.com/robots.txt", b"", status=404)
    s.add("https://thinblog.com/", b"<html><body>Thin blog home</body></html>", content_type=html)
    s.add("https://thinblog.com/feed", fixture_bytes("thinblog_feed.xml"), content_type=rss)
    s.add("https://thinblog.com/posts/ultimate-guide-technical-seo", fixture_bytes("article_thin.html"),
          content_type=html)
    return s


@pytest.fixture
def scan_config(base_config):
    cfg = base_config
    cfg["keywords"] = ["technical seo", "content marketing"]
    cfg["competitor_domains"] = ["goodblog.com"]
    cfg["discovery"].update({
        "window_hours": 168, "feeds": [], "sitemaps": [], "max_urls": 50, "max_urls_per_domain": 25,
        "filter_by_keywords": True, "reprocess_seen_urls": False, "max_search_queries": 1,
        "methods": {"rss": True, "sitemap": True, "seed_domains": True, "search": True},
        "search_sources": {"google_news_rss": True},
    })
    cfg["topics"]["synonyms_file"] = None
    return cfg


def read_csv(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def by_url(rows):
    return {r["url"].split("?")[0]: r for r in rows}


def test_full_pipeline(scan_config):
    session = mini_web()
    result = ScanRunner(scan_config, session=session, resolver=public_resolver, now=NOW).run()
    assert result["state"] == "completed", result.get("message")
    d, r = result["discovery"], result["results"]

    # Discovery: search + feeds + sitemaps all contributed; old/non-article URLs rejected pre-fetch.
    assert d["methods"]["search"]["ok"] and d["methods"]["seed_domains"]["ok"]
    assert d["search_sources"][0]["available"]
    rejected = d["rejected_before_fetch"]
    assert rejected.get("published_before_window") == 1  # old link-building post
    assert rejected.get("not_article_like", 0) >= 1      # /category/seo
    assert "https://goodblog.com/blog/ancient-post-2019" not in session.calls
    assert "https://goodblog.com/blog/old-link-building-post" not in session.calls

    files = result["exports"]
    all_rows = by_url(read_csv(files["all_articles"]))
    good = all_rows["https://goodblog.com/blog/technical-seo-audit-large-sites"]
    assert good["date_status"] == "confirmed_recent" and good["date_confidence"] == "high"
    assert good["publication_date"] == "2026-10-09T08:15:00Z"
    assert good["modification_date"] == "2026-10-09T10:00:00Z"
    assert float(good["quality_score"]) >= 70
    assert good["user_keyword"] == "technical seo"
    assert "seed_feed" in good["discovery_source"] and "seed_sitemap" in good["discovery_source"]
    assert good["canonical_url"] == "https://goodblog.com/blog/technical-seo-audit-large-sites"

    thin = all_rows["https://thinblog.com/posts/ultimate-guide-technical-seo"]
    assert thin["discovery_source"] == "search:google_news_rss"
    assert thin["discovery_query"] == "technical seo"
    assert float(thin["quality_score"]) < 40 and thin["explanation"]

    # Observed positions in the Google News RSS results (fixture order: copy #1, thin #2).
    assert thin["search_position_best"] == "2" and thin["search_result_page"].startswith("page 1")
    assert 'Google News RSS "technical seo" #2' in thin["search_position_details"]
    assert all_rows["https://copyblog.com/technical-seo-audit-process"]["search_position_best"] == "1"
    assert good["search_position_best"] == ""  # not found via a ranked search source -> no position invented

    copy = all_rows["https://copyblog.com/technical-seo-audit-process"]
    assert "similar" in copy["quality_flags"] or "duplicate" in copy["quality_flags"]

    assert all_rows["https://goodblog.com/blog/content-marketing-budgets-2026"]["date_status"] == "uncertain"
    assert "conflicting" in all_rows["https://goodblog.com/blog/content-marketing-budgets-2026"]["date_flags"]
    assert all_rows["https://goodblog.com/blog/technical-seo-basics-updated"]["date_status"] == "outside_window"
    assert all_rows["https://goodblog.com/news/google-october-core-update"]["extraction_status"] == \
        "skipped_no_keyword_match"
    assert all_rows["https://goodblog.com/blog/undated-technical-seo-note"]["extraction_status"] == "fetch_failed"

    confirmed = by_url(read_csv(files["confirmed_recent"]))
    assert "https://goodblog.com/blog/technical-seo-audit-large-sites" in confirmed
    assert "https://goodblog.com/blog/technical-seo-basics-updated" not in confirmed
    low = by_url(read_csv(files["low_quality"]))
    assert "https://thinblog.com/posts/ultimate-guide-technical-seo" in low
    assert "https://goodblog.com/blog/technical-seo-audit-large-sites" not in low
    review = by_url(read_csv(files["needs_date_review"]))
    assert {"https://goodblog.com/blog/content-marketing-budgets-2026",
            "https://goodblog.com/blog/content-checklist-saas"} <= set(review)
    failed = by_url(read_csv(files["failed_extractions"]))
    assert "https://goodblog.com/blog/undated-technical-seo-note" in failed

    assert r["confirmed_recent"] == len(confirmed) and r["flagged_low_quality"] == len(low)
    assert r["outside_window_rejected"] == 1

    # Second scan: already-processed URLs are not fetched again.
    session2 = mini_web()
    result2 = ScanRunner(scan_config, session=session2, resolver=public_resolver, now=NOW).run()
    assert result2["discovery"]["rejected_before_fetch"].get("already_processed", 0) >= 5
    assert "https://goodblog.com/blog/technical-seo-audit-large-sites" not in session2.calls
    db = Storage(scan_config["paths"]["data_dir"] + "/scanner.db")
    assert [s["status"] for s in db.list_scans()] == ["completed", "completed"]


def test_stop_and_resume(scan_config):
    runner_events = []

    def progress(status):
        runner_events.append(status)
        if status.get("phase") == "extraction" and status.get("processed") == 2 and not request_stop.called:
            request_stop.called = True
            request_stop(scan_config)

    request_stop.called = False
    result = ScanRunner(scan_config, session=mini_web(), resolver=public_resolver, now=NOW, progress=progress).run()
    assert result["state"] == "stopped"
    db = Storage(scan_config["paths"]["data_dir"] + "/scanner.db")
    scan_id = result["scan_id"]
    pending_after_stop = len(db.queue_items(scan_id, "pending"))
    assert pending_after_stop > 0
    assert db.get_scan(scan_id)["status"] == "stopped"

    resumed = ScanRunner(scan_config, session=mini_web(), resolver=public_resolver, now=NOW).run(resume=True)
    assert resumed["scan_id"] == scan_id and resumed["state"] == "completed"
    assert db.queue_items(scan_id, "pending") == []
    assert resumed["results"]["confirmed_recent"] >= 1


def test_overlapping_scans_prevented(scan_config):
    from scanner.config import resolve_path

    lock = ScanLock(resolve_path(scan_config, "data_dir") / "scan.lock")
    lock.acquire()
    try:
        assert lock.is_locked()
        with pytest.raises(ScanAlreadyRunning):
            ScanRunner(scan_config, session=mini_web(), resolver=public_resolver, now=NOW).run()
    finally:
        lock.release()
    assert not lock.is_locked()


def test_all_methods_failing_still_completes(scan_config):
    """Every site down: the scan finishes, reports problems and writes (empty) exports."""
    scan_config["http"]["max_retries"] = 0
    result = ScanRunner(scan_config, session=FakeSession(default_status=503), resolver=public_resolver, now=NOW).run()
    assert result["state"] == "completed"
    assert result["results"]["urls_processed"] == 0
    assert not result["discovery"]["search_sources"][0]["available"]


def test_search_position_fields_average_across_scans():
    from scanner.pipeline import search_position_fields

    current = [{"source": "Google News RSS", "query": "seo", "rank": 14, "observed_at": "2026-10-11T12:00:00Z"}]
    history = current + [{"source": "Google News RSS", "query": "seo", "rank": 6, "observed_at": "2026-10-10T12:00:00Z"},
                         {"source": "Bing News RSS", "query": "seo", "rank": 10, "observed_at": "2026-10-09T12:00:00Z"}]
    f = search_position_fields(current, history)
    assert f["search_position_best"] == 14 and f["search_position_avg"] == 10.0
    assert f["search_result_page"].startswith("page 2") and f["search_position_observations"] == 3
    assert search_position_fields([], [])["search_position_best"] == ""
