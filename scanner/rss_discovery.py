"""Method A: RSS / Atom feed discovery and parsing."""

from __future__ import annotations

import calendar
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
from bs4 import BeautifulSoup

from .date_verification import DateEvidence, make_evidence
from .http_client import Fetcher
from .models import DiscoveredItem

log = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json",
              "application/rdf+xml", "application/xml", "text/xml")
COMMON_FEED_PATHS = (
    "/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feed/",
    "/blog/feed", "/blog/rss", "/blog/rss.xml", "/blog/feed.xml", "/blog/atom.xml",
    "/feeds/posts/default", "/rss/index.rss", "/news/feed",
)


@dataclass
class FeedParseResult:
    items: list[DiscoveredItem] = field(default_factory=list)
    next_url: str | None = None
    is_wordpress: bool = False
    title: str = ""
    error: str | None = None


def _clean_html(text: str, limit: int = 600) -> str:
    if not text:
        return ""
    if "<" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _struct_to_dt(st) -> datetime | None:
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def _entry_evidence(entry, source_prefix: str = "feed") -> list[DateEvidence]:
    out: list[DateEvidence] = []
    keys = set(entry.keys())
    for raw_key, parsed_key, src in (
        ("published", "published_parsed", f"{source_prefix}_published"),
        ("created", "created_parsed", f"{source_prefix}_published"),
        ("updated", "updated_parsed", f"{source_prefix}_updated"),
    ):
        if raw_key not in keys and parsed_key not in keys:
            continue
        kind = "modified" if src.endswith("_updated") else "published"
        ev = make_evidence(src, entry.get(raw_key), kind=kind) if entry.get(raw_key) else None
        if ev is None:
            dt = _struct_to_dt(entry.get(parsed_key))
            if dt:
                ev = DateEvidence(source=src, kind=kind, value=dt, raw=str(entry.get(raw_key, ""))[:80])
        if ev:
            out.append(ev)
    return out


def _entry_link(entry, feed_url: str) -> str:
    link = entry.get("link") or ""
    if not link:
        for l in entry.get("links", []) or []:
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l["href"]
                break
    if not link:
        guid = entry.get("id") or ""
        if guid.startswith("http"):
            link = guid
    return urljoin(feed_url, link) if link else ""


def parse_feed(content: bytes | str, feed_url: str, source_label: str | None = None) -> FeedParseResult:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom content. Never raises."""
    result = FeedParseResult()
    raw = content.encode("utf-8") if isinstance(content, str) else (content or b"")
    if not raw.strip():
        result.error = "empty feed"
        return result
    try:
        parsed = feedparser.parse(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - feedparser can raise on pathological input
        result.error = f"feed parse error: {exc}"
        return result
    entries = parsed.get("entries") or []
    if not entries and not parsed.get("version"):
        result.error = "not a valid feed (no RSS/Atom root element)"
        return result
    if parsed.get("bozo") and not entries:
        version = parsed.get("version") or ""
        result.error = "not a valid feed" if not version else f"malformed feed: {parsed.get('bozo_exception')}"
        return result
    feed_meta = parsed.get("feed") or {}
    result.title = feed_meta.get("title", "")
    generator = (feed_meta.get("generator") or "") + " " + str(feed_meta.get("generator_detail", ""))
    result.is_wordpress = "wordpress" in generator.lower()
    for l in feed_meta.get("links", []) or []:
        if l.get("rel") == "next" and l.get("href"):
            result.next_url = urljoin(feed_url, l["href"])
    label = source_label or f"rss:{feed_url}"
    for entry in entries:
        link = _entry_link(entry, feed_url)
        if not link.startswith(("http://", "https://")):
            continue
        result.items.append(
            DiscoveredItem(
                url=link,
                title=_clean_html(entry.get("title", ""), 300),
                summary=_clean_html(entry.get("summary", "") or entry.get("description", "")),
                discovery_source=label,
                date_evidence=_entry_evidence(entry),
            )
        )
    return result


def _wordpress_page_url(feed_url: str, page: int) -> str:
    parts = urlsplit(feed_url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "paged"]
    query.append(("paged", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def fetch_feed(
    fetcher: Fetcher,
    feed_url: str,
    window_start: datetime | None = None,
    max_pages: int = 3,
    source_label: str | None = None,
) -> tuple[list[DiscoveredItem], list[str]]:
    """Fetch a feed and follow pagination while items are still inside the window.

    Returns (items, log_messages).
    """
    items: list[DiscoveredItem] = []
    messages: list[str] = []
    url: str | None = feed_url
    page = 1
    seen_pages = set()
    while url and page <= max(1, max_pages) and url not in seen_pages:
        seen_pages.add(url)
        res = fetcher.fetch(url)
        if not res.ok:
            messages.append(f"feed {url}: {res.error or res.status}")
            break
        parsed = parse_feed(res.content, res.final_url or url, source_label or f"rss:{feed_url}")
        if parsed.error:
            messages.append(f"feed {url}: {parsed.error}")
            break
        items.extend(parsed.items)
        messages.append(f"feed {url}: {len(parsed.items)} entries")
        if not parsed.items or window_start is None:
            break
        # Only paginate if every entry on this page is still recent (older pages can't add new posts).
        dated = [min((e.value for e in it.date_evidence if e.kind == "published"), default=None)
                 for it in parsed.items]
        if any(d is None or d < window_start for d in dated):
            break
        page += 1
        if parsed.next_url:
            url = parsed.next_url
        elif parsed.is_wordpress:
            url = _wordpress_page_url(feed_url, page)
        else:
            url = None
    return items, messages


def looks_like_feed(content: bytes) -> bool:
    head = content[:1500].lstrip().lower()
    return any(tag in head for tag in (b"<rss", b"<feed", b"<rdf:rdf")) or (
        head.startswith(b"{") and b"jsonfeed.org" in head
    )


def find_feed_links_in_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    found = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        typ = (link.get("type") or "").lower()
        if "alternate" in rel and typ in FEED_TYPES[:4]:
            href = urljoin(base_url, link["href"])
            title = (link.get("title") or "").lower()
            if "comment" in title or "/comments/" in href:
                continue
            found.append(href)
    return list(dict.fromkeys(found))


def discover_feeds(fetcher: Fetcher, base_url: str, max_probes: int = 6) -> tuple[list[str], list[str]]:
    """Feed discovery helper for a user-supplied domain or blog URL.

    1. <link rel="alternate"> tags on the given page (and site homepage)
    2. common feed locations (/feed, /rss.xml, /atom.xml, ...)
    Returns (feed_urls, log_messages).
    """
    messages: list[str] = []
    feeds: list[str] = []
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    pages = [base_url] if base_url.rstrip("/") == origin else [base_url, origin + "/"]
    for page in pages:
        res = fetcher.fetch(page)
        if res.ok and "html" in (res.content_type or "text/html"):
            links = find_feed_links_in_html(res.text, res.final_url or page)
            feeds.extend(links)
            messages.append(f"{page}: {len(links)} feed link(s) in HTML")
        else:
            messages.append(f"{page}: {res.error or res.status}")
    if not feeds:
        prefix = base_url.rstrip("/") if base_url.rstrip("/") != origin else origin
        candidates = [prefix + p for p in COMMON_FEED_PATHS]
        if prefix != origin:
            candidates = [prefix + "/feed", prefix + "/rss.xml", prefix + "/feed.xml"] + [origin + p for p in COMMON_FEED_PATHS]
        probes = 0
        for cand in dict.fromkeys(candidates):
            if probes >= max_probes:
                break
            probes += 1
            res = fetcher.fetch(cand)
            if res.ok and looks_like_feed(res.content):
                feeds.append(res.final_url or cand)
                messages.append(f"found feed at common location {cand}")
                break
    return list(dict.fromkeys(feeds)), messages
