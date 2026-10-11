"""Method C: free, unauthenticated keyword search sources.

Evaluation (see README "Search sources" for details):

  * Search-engine HTML result pages (Google, Bing, DuckDuckGo web results) are NOT
    used. Their terms prohibit automated querying and they deploy anti-bot
    systems/CAPTCHAs that this tool must not bypass.
  * Google News RSS and Bing News RSS are public RSS endpoints intended for feed
    readers. They are used only if robots.txt allows it, at a slow rate, and are
    probed at the start of every scan. Coverage is limited to news-indexed sites.
  * Medium / DEV / WordPress.com tag feeds are public RSS feeds of blog posts
    filed under a tag. They only cover those platforms.
  * Hacker News (Algolia) search is a free public API; it finds links people
    submitted, not all blogs.

None of these is exhaustive. If a source is blocked, rate limited, disallowed by
robots.txt or returns something that is not a feed, it is disabled for the scan
and the reason is reported. Nothing is simulated.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

from .http_client import Fetcher, FetchResult
from .models import DiscoveredItem
from .rss_discovery import looks_like_feed, parse_feed
from .topics import Query

log = logging.getLogger(__name__)


@dataclass
class SourceStatus:
    name: str
    available: bool
    message: str


class SearchSource:
    name = "base"
    label = "Base"
    # True only for sources whose results are ordered by relevance. Their result
    # order is recorded as an observed position; recency-ordered feeds get none.
    ranked = False
    accepts_origins = ("user", "synonym", "combination", "variation", "mined_title")
    coverage_note = ""

    def build_url(self, query: str, window_hours: float) -> str | None:
        raise NotImplementedError

    def parse(self, res: FetchResult, query: Query) -> tuple[list[DiscoveredItem], str | None]:
        parsed = parse_feed(res.content, res.final_url or res.url, source_label=f"search:{self.name}")
        if parsed.error:
            return [], parsed.error
        for item in parsed.items:
            self._postprocess(item)
        return parsed.items, None

    def _postprocess(self, item: DiscoveredItem) -> None:
        pass

    def diagnose(self, res: FetchResult) -> str | None:
        """Return a reason if the response means the source is unusable."""
        if res.error:
            if res.error.startswith("robots"):
                return f"disallowed: {res.error}"
            if res.status in (401, 403):
                return f"blocked (HTTP {res.status})"
            if res.status == 429:
                return "rate limited (HTTP 429)"
            return res.error
        if not looks_like_feed(res.content):
            low = res.content[:5000].lower()
            if b"captcha" in low or b"unusual traffic" in low or b"consent" in low:
                return "returned a CAPTCHA/consent page instead of a feed - disabled (not bypassed)"
            return "did not return a feed"
        return None


class GoogleNewsRSS(SearchSource):
    name = "google_news_rss"
    label = "Google News RSS"
    ranked = True
    accepts_origins = ("user", "synonym", "combination", "variation", "mined_title")
    coverage_note = "News-indexed publishers only; links may be Google redirect URLs."

    def build_url(self, query, window_hours):
        days = max(1, math.ceil(window_hours / 24))
        q = quote_plus(f"{query} when:{days}d")
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

    def _postprocess(self, item):
        for ev in item.date_evidence:
            if ev.source == "feed_published":
                ev.source = "search_result_date"
        decoded = decode_google_news_url(item.url)
        if decoded:
            item.url = decoded
        elif "news.google.com" in item.url:
            item.resolved = False
        # Google News titles are "Headline - Publisher".
        item.title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item.title)

    def parse(self, res, query):
        items, err = super().parse(res, query)
        if err:
            return items, err
        # <source url="https://publisher.example">Publisher</source>
        sources = re.findall(rb'<source url="([^"]+)"', res.content)
        for item, src in zip(items, sources):
            item.publisher_url = src.decode("utf-8", "replace")
        return items, None


class BingNewsRSS(SearchSource):
    name = "bing_news_rss"
    label = "Bing News RSS"
    ranked = True
    coverage_note = "News-indexed publishers only."

    def build_url(self, query, window_hours):
        return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"

    def _postprocess(self, item):
        for ev in item.date_evidence:
            if ev.source == "feed_published":
                ev.source = "search_result_date"
        parts = urlsplit(item.url)
        if parts.hostname and parts.hostname.endswith("bing.com"):
            target = parse_qs(parts.query).get("url", [""])[0]
            if target.startswith("http"):
                item.url = target
            else:
                item.resolved = False


def _slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")


class MediumTagFeed(SearchSource):
    name = "medium_tag_feed"
    label = "Medium tag feed"
    accepts_origins = ("user", "synonym", "combination")
    coverage_note = "Medium posts only; member-only posts show a preview."

    def build_url(self, query, window_hours):
        return f"https://medium.com/feed/tag/{quote(_slug(query))}"


class DevToTagFeed(SearchSource):
    name = "devto_tag_feed"
    label = "DEV tag feed"
    accepts_origins = ("user", "synonym", "combination")
    coverage_note = "dev.to posts only (software topics)."

    def build_url(self, query, window_hours):
        tag = re.sub(r"[^a-z0-9]", "", query.lower())
        return f"https://dev.to/feed/tag/{tag}" if tag else None


class WordPressTagFeed(SearchSource):
    name = "wordpress_tag_feed"
    label = "WordPress.com tag feed (experimental)"
    accepts_origins = ("user", "synonym", "combination")
    coverage_note = "WordPress.com-hosted blogs only; endpoint not officially documented."

    def build_url(self, query, window_hours):
        return f"https://wordpress.com/tag/{quote(_slug(query))}/feed/"


class HackerNewsAlgolia(SearchSource):
    name = "hn_algolia"
    label = "Hacker News search (Algolia)"
    coverage_note = "Only links submitted to Hacker News; submission time is not publication time."

    def build_url(self, query, window_hours):
        since = int(time.time() - window_hours * 3600)
        return (f"https://hn.algolia.com/api/v1/search_by_date?query={quote_plus(query)}"
                f"&tags=story&numericFilters=created_at_i>{since}&hitsPerPage=50")

    def diagnose(self, res):
        if res.error:
            return super().diagnose(res)
        try:
            json.loads(res.content)
        except ValueError:
            return "did not return JSON"
        return None

    def parse(self, res, query):
        try:
            data = json.loads(res.content)
        except ValueError:
            return [], "invalid JSON"
        items = []
        for hit in data.get("hits", []):
            url = hit.get("url") or ""
            if not url.startswith("http"):
                continue
            # Submission time is NOT publication evidence; recorded only in the source label.
            items.append(DiscoveredItem(url=url, title=hit.get("title") or "",
                                        discovery_source=f"search:{self.name}"))
        return items, None


ALL_SOURCES = {cls.name: cls for cls in (GoogleNewsRSS, BingNewsRSS, MediumTagFeed, DevToTagFeed,
                                         WordPressTagFeed, HackerNewsAlgolia)}


def decode_google_news_url(url: str) -> str | None:
    """Decode legacy Google News article URLs that embed the target URL in base64.

    Newer Google News URLs are opaque and can only be resolved through an
    undocumented internal endpoint; we do not call it. Those items are resolved
    via the publisher's own feed instead (see discovery.resolve_unresolved).
    """
    parts = urlsplit(url)
    if parts.hostname != "news.google.com":
        return None
    m = re.search(r"/articles/([^/?#]+)", parts.path)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, base64.binascii.Error):
        return None
    found = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not found:
        return None
    candidate = found.group(0).decode("ascii", "ignore")
    # Strip trailing protobuf bytes that happen to be printable.
    candidate = re.split(r"[\x00-\x1f\"<>\\^`{|}]", candidate)[0]
    if "news.google.com" in candidate or len(candidate) < 12:
        return None
    return candidate


def run_search_sources(
    fetcher: Fetcher,
    enabled: dict[str, bool],
    queries: list[Query],
    window_hours: float,
    max_queries: int = 6,
    delay_seconds: float = 5.0,
    sleep=time.sleep,
) -> tuple[list[DiscoveredItem], list[SourceStatus], list[str]]:
    """Probe each enabled source with its first query, then run remaining queries."""
    items: list[DiscoveredItem] = []
    statuses: list[SourceStatus] = []
    messages: list[str] = []
    for name, cls in ALL_SOURCES.items():
        if not enabled.get(name):
            continue
        source = cls()
        usable = [q for q in queries if q.origin in source.accepts_origins][:max(1, max_queries)]
        if not usable:
            statuses.append(SourceStatus(source.label, False, "no suitable queries"))
            continue
        working = True
        count = 0
        for i, query in enumerate(usable):
            url = source.build_url(query.text, window_hours)
            if not url:
                continue
            if i > 0:
                sleep(delay_seconds)
            res = fetcher.fetch(url, use_cache=True)
            problem = source.diagnose(res)
            if problem:
                if i == 0:
                    statuses.append(SourceStatus(source.label, False, problem))
                    messages.append(f"{source.label}: disabled for this scan - {problem}")
                    working = False
                else:
                    messages.append(f"{source.label}: stopped after query '{query.text}' - {problem}")
                break
            found, err = source.parse(res, query)
            if err:
                messages.append(f"{source.label} '{query.text}': {err}")
                continue
            observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for rank, item in enumerate(found, start=1):
                item.discovery_query = query.text
                item.query_origin = query.origin
                item.user_keyword = query.user_keyword
                if source.ranked:
                    item.search_ranks.append({"source": source.label, "query": query.text, "rank": rank,
                                              "observed_at": observed_at})
            items.extend(found)
            count += len(found)
            messages.append(f"{source.label} '{query.text}' ({query.origin_label}): {len(found)} results")
        if working:
            statuses.append(SourceStatus(source.label, True, f"{count} results. {source.coverage_note}"))
    return items, statuses, messages


def probe_sources(fetcher: Fetcher, enabled: dict[str, bool] | None = None,
                  test_query: str = "content marketing") -> list[SourceStatus]:
    """Check which search sources currently respond with usable data (for the UI / CLI)."""
    out = []
    for name, cls in ALL_SOURCES.items():
        if enabled is not None and not enabled.get(name):
            continue
        source = cls()
        url = source.build_url(test_query, 168)
        res = fetcher.fetch(url, use_cache=False)
        problem = source.diagnose(res)
        if problem:
            out.append(SourceStatus(source.label, False, problem))
            continue
        items, err = source.parse(res, Query(test_query, "user", test_query))
        if err:
            out.append(SourceStatus(source.label, False, err))
        else:
            unresolved = sum(1 for i in items if not i.resolved)
            note = f"; {unresolved} redirect link(s) need resolving" if unresolved else ""
            out.append(SourceStatus(source.label, True, f"OK - {len(items)} results for '{test_query}'{note}. "
                                                         f"{source.coverage_note}"))
    return out
