"""Method B: XML sitemap discovery (robots.txt, sitemap indexes, nested sitemaps, news sitemaps).

<lastmod> is used only as a discovery *hint* (stored as modification evidence),
never as a publication date. Google News sitemaps' <news:publication_date> is
stored as medium-reliability publication evidence and still cross-checked
against the article page.
"""

from __future__ import annotations

import gzip
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from lxml import etree

from .date_verification import make_evidence
from .http_client import Fetcher
from .models import DiscoveredItem

log = logging.getLogger(__name__)

PRIORITY_HINTS = ("news", "post", "blog", "article", "story", "stories")
DEPRIORITIZE_HINTS = ("page", "category", "categories", "tag", "author", "product", "image",
                      "video", "attachment", "media", "user", "location", "taxonomy")


@dataclass
class SitemapEntry:
    loc: str
    lastmod: str = ""
    news_publication_date: str = ""
    news_title: str = ""


@dataclass
class SitemapParseResult:
    kind: str = ""  # "urlset" | "sitemapindex"
    entries: list[SitemapEntry] = field(default_factory=list)
    error: str | None = None


def sitemaps_from_robots(robots_text: str, base_url: str) -> list[str]:
    found = []
    for line in robots_text.splitlines():
        m = re.match(r"^\s*sitemap\s*:\s*(\S+)", line, re.I)
        if m:
            found.append(urljoin(base_url, m.group(1).strip()))
    return list(dict.fromkeys(found))


def _maybe_decompress(content: bytes, max_bytes: int) -> bytes:
    if content[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
            data = gz.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"decompressed sitemap exceeds {max_bytes} bytes")
        return data
    return content


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_sitemap(content: bytes, max_bytes: int = 10_000_000) -> SitemapParseResult:
    """Parse a urlset or sitemapindex document. Never raises."""
    result = SitemapParseResult()
    if not content or not content.strip():
        result.error = "empty sitemap"
        return result
    try:
        data = _maybe_decompress(content, max_bytes)
    except (OSError, ValueError, EOFError) as exc:
        result.error = f"bad gzip: {exc}"
        return result
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        result.error = f"malformed XML: {exc}"
        return result
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        result.error = f"not a sitemap (root element <{kind}>)"
        return result
    result.kind = kind
    child_tag = "url" if kind == "urlset" else "sitemap"
    for node in root:
        if _local(node.tag) != child_tag:
            continue
        entry = SitemapEntry(loc="")
        for child in node.iter():
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name == "loc" and not entry.loc and child.getparent() is node:
                entry.loc = text
            elif name == "lastmod" and child.getparent() is node:
                entry.lastmod = text
            elif name == "publication_date":
                entry.news_publication_date = text
            elif name == "title" and _local(child.getparent().tag) == "news":
                entry.news_title = text
        if entry.loc:
            result.entries.append(entry)
    return result


def _priority(url: str) -> int:
    path = urlsplit(url).path.lower()
    if any(h in path for h in PRIORITY_HINTS):
        return 0
    if any(h in path for h in DEPRIORITIZE_HINTS):
        return 2
    return 1


def discover_from_sitemaps(
    fetcher: Fetcher,
    sitemap_urls: list[str],
    window_start: datetime,
    max_bytes: int = 10_000_000,
    max_sitemaps: int = 8,
    max_depth: int = 2,
    source_label: str = "sitemap",
) -> tuple[list[DiscoveredItem], list[str]]:
    """Walk sitemaps breadth-first and return URLs with recent lastmod/news dates.

    Entries with no date information at all are skipped: a sitemap of thousands of
    undated URLs cannot be narrowed to recent posts without fetching each page.
    """
    items: list[DiscoveredItem] = []
    messages: list[str] = []
    queue: list[tuple[str, int]] = [(u, 0) for u in sitemap_urls]
    visited: set[str] = set()
    fetched = 0
    undated = 0
    while queue and fetched < max_sitemaps:
        queue.sort(key=lambda t: (t[1], _priority(t[0])))
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        res = fetcher.fetch(url, max_bytes=max_bytes)
        fetched += 1
        if not res.ok:
            messages.append(f"sitemap {url}: {res.error or res.status}")
            continue
        parsed = parse_sitemap(res.content, max_bytes=max_bytes)
        if parsed.error:
            messages.append(f"sitemap {url}: {parsed.error}")
            continue
        if parsed.kind == "sitemapindex":
            added = 0
            for entry in parsed.entries:
                if depth + 1 > max_depth:
                    break
                ev = make_evidence("sitemap_lastmod", entry.lastmod) if entry.lastmod else None
                # A child sitemap last modified before the window cannot contain newer URLs.
                if ev and ev.value < window_start:
                    continue
                queue.append((urljoin(url, entry.loc), depth + 1))
                added += 1
            messages.append(f"sitemap index {url}: {len(parsed.entries)} children, {added} queued")
            continue
        recent = 0
        for entry in parsed.entries:
            evidence = []
            pub = make_evidence("news_sitemap_publication_date", entry.news_publication_date, kind="published") \
                if entry.news_publication_date else None
            mod = make_evidence("sitemap_lastmod", entry.lastmod, kind="modified") if entry.lastmod else None
            if pub:
                evidence.append(pub)
            if mod:
                evidence.append(mod)
            if not evidence:
                undated += 1
                continue
            newest = max(e.value for e in evidence)
            if newest < window_start:
                continue
            items.append(DiscoveredItem(url=urljoin(url, entry.loc), title=entry.news_title,
                                        discovery_source=f"{source_label}:{url}", date_evidence=evidence))
            recent += 1
        messages.append(f"sitemap {url}: {len(parsed.entries)} URLs, {recent} with recent dates")
    if queue:
        messages.append(f"sitemap limit reached ({max_sitemaps}); {len(queue)} sitemap(s) not fetched")
    if undated:
        messages.append(f"{undated} sitemap URL(s) skipped because they had no lastmod/publication date")
    return items, messages


def find_sitemaps(fetcher: Fetcher, base_url: str) -> tuple[list[str], list[str]]:
    """Sitemaps listed in robots.txt, otherwise the conventional /sitemap.xml locations."""
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    robots = fetcher.robots_text(origin + "/")
    found = sitemaps_from_robots(robots, origin)
    if found:
        return found, [f"{origin}: {len(found)} sitemap(s) in robots.txt"]
    return [origin + "/sitemap_index.xml", origin + "/sitemap.xml"], [
        f"{origin}: no Sitemap line in robots.txt, trying conventional locations"
    ]
