"""Optional live check against a few public websites.

Skipped by default so the main suite never depends on the network. Run with:
    RUN_LIVE_TESTS=1 python -m pytest tests/test_integration_live.py -v -s
A failure here usually means a site changed, blocked automated access, or your
network is offline - not necessarily a bug in the tool.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from scanner.http_client import Fetcher
from scanner.rss_discovery import discover_feeds, fetch_feed
from scanner.search_discovery import probe_sources

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to run")]

HTTP = {"user_agent": "BlogQualityScanner/1.0 (+local SEO research tool; integration test)",
        "min_delay_seconds": 2, "timeout_seconds": 20, "cache_ttl_hours": 0}


@pytest.mark.parametrize("domain", ["https://wordpress.org/news/", "https://blog.python.org/"])
def test_feed_discovery_on_public_blog(domain):
    fetcher = Fetcher(HTTP)
    feeds, msgs = discover_feeds(fetcher, domain)
    assert feeds, msgs
    items, _ = fetch_feed(fetcher, feeds[0], datetime.now(timezone.utc) - timedelta(days=3650), max_pages=1)
    assert items and all(i.url.startswith("http") for i in items)


def test_search_sources_report_status():
    statuses = probe_sources(Fetcher(HTTP))
    for s in statuses:
        print(("OK  " if s.available else "FAIL"), s.name, "-", s.message)
    assert statuses  # every source reports a status (available or a reason) - never simulated
