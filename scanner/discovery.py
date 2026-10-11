"""Discovery orchestrator: runs every enabled method independently, merges and filters results.

A failure in one method (or one feed/sitemap/domain) is logged and never stops the others.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable


from .date_verification import RANK, SOURCE_RELIABILITY
from .http_client import Fetcher, StopRequested
from .models import DiscoveredItem
from .rss_discovery import discover_feeds, fetch_feed
from .search_discovery import SourceStatus, run_search_sources
from .sitemap_discovery import discover_from_sitemaps, find_sitemaps
from .topics import Query, expand_keywords, match_keywords, mine_title_phrases
from .urlutils import date_from_url, domain_to_base_url, get_domain, is_article_like, normalize_url

log = logging.getLogger(__name__)


@dataclass
class MethodReport:
    name: str
    enabled: bool
    ok: bool = True
    found: int = 0
    messages: list[str] = field(default_factory=list)


@dataclass
class DiscoveryReport:
    candidates: list[DiscoveredItem] = field(default_factory=list)
    methods: dict[str, MethodReport] = field(default_factory=dict)
    search_statuses: list[SourceStatus] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    suggested_phrases: list[Query] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)
    total_found: int = 0
    unique_found: int = 0

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.rejected:
            key = reason.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
        return counts


def _run_method(report: MethodReport, fn: Callable[[], list[DiscoveredItem]]) -> list[DiscoveredItem]:
    try:
        items = fn()
        report.found += len(items)
        return items
    except StopRequested:
        raise
    except Exception as exc:  # noqa: BLE001 - a method must never crash the scan
        report.ok = False
        report.messages.append(f"error: {type(exc).__name__}: {exc}")
        log.exception("discovery method %s failed", report.name)
        return []


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip()


def resolve_unresolved(fetcher: Fetcher, items: list[DiscoveredItem], window_start: datetime,
                       max_domains: int = 10) -> tuple[list[DiscoveredItem], list[str]]:
    """Resolve opaque search redirect links (e.g. new-style Google News URLs) by finding the
    same title in the publisher's own RSS feed. No undocumented endpoints are used."""
    messages = []
    by_domain: dict[str, list[DiscoveredItem]] = {}
    for it in items:
        if it.publisher_url:
            by_domain.setdefault(it.publisher_url, []).append(it)
    resolved: list[DiscoveredItem] = []
    for i, (publisher, group) in enumerate(by_domain.items()):
        if i >= max_domains:
            messages.append(f"{len(by_domain) - max_domains} publisher(s) not checked (limit {max_domains})")
            break
        feeds, _ = discover_feeds(fetcher, domain_to_base_url(publisher), max_probes=3)
        entries: list[DiscoveredItem] = []
        for feed in feeds[:2]:
            got, _ = fetch_feed(fetcher, feed, window_start, max_pages=1)
            entries.extend(got)
        keys = {_title_key(e.title): e for e in entries if e.title}
        for it in group:
            target = _title_key(it.title)
            match = keys.get(target)
            if not match and keys:
                close = difflib.get_close_matches(target, list(keys), n=1, cutoff=0.85)
                match = keys[close[0]] if close else None
            if match:
                it.url = match.url
                it.resolved = True
                it.date_evidence.extend(match.date_evidence)
                resolved.append(it)
        messages.append(f"{publisher}: resolved {sum(1 for g in group if g.resolved)}/{len(group)} via publisher feed")
    return resolved, messages


def run_discovery(config: dict, fetcher: Fetcher, now: datetime, storage=None,
                  progress: Callable[[str], None] | None = None) -> DiscoveryReport:
    progress = progress or (lambda msg: None)
    disc = config.get("discovery") or {}
    methods = disc.get("methods") or {}
    window_hours = float(disc.get("window_hours", 168))
    window_start = now - timedelta(hours=window_hours)
    report = DiscoveryReport()
    report.queries = expand_keywords(config.get("keywords") or [], config.get("topics") or {})
    collected: list[DiscoveredItem] = []
    max_pages = int(disc.get("max_feed_pages", 3))
    sm_kwargs = dict(max_bytes=int(disc.get("max_sitemap_bytes", 10_000_000)),
                     max_sitemaps=int(disc.get("max_sitemaps_per_domain", 8)),
                     max_depth=int(disc.get("max_sitemap_depth", 2)))

    # ---- Method C: keyword search sources (primary, keyword-driven)
    rep = report.methods.setdefault("search", MethodReport("Keyword search sources (C)", bool(methods.get("search"))))
    if rep.enabled and report.queries:
        progress("Searching free keyword sources")

        def _search():
            items, statuses, msgs = run_search_sources(
                fetcher, disc.get("search_sources") or {}, report.queries, window_hours,
                max_queries=int(disc.get("max_search_queries", 6)),
                delay_seconds=float(disc.get("search_delay_seconds", 5)),
            )
            report.search_statuses = statuses
            rep.messages.extend(msgs)
            if not any(s.available for s in statuses):
                rep.ok = False
            unresolved = [i for i in items if not i.resolved]
            if unresolved:
                progress(f"Resolving {len(unresolved)} search redirect link(s) via publisher feeds")
                fixed, rmsgs = resolve_unresolved(fetcher, unresolved, window_start)
                rep.messages.extend(rmsgs)
                for it in unresolved:
                    if not it.resolved:
                        report.rejected.append((it.url, f"unresolved_search_redirect: {it.title[:80]}"))
            return [i for i in items if i.resolved]

        collected += _run_method(rep, _search)
    elif rep.enabled:
        rep.messages.append("no keywords configured")

    # ---- Method A: configured feeds
    rep = report.methods.setdefault("rss", MethodReport("RSS/Atom feeds (A)", bool(methods.get("rss"))))
    if rep.enabled:
        for feed in disc.get("feeds") or []:
            progress(f"Reading feed {feed}")

            def _feed(feed=feed):
                items, msgs = fetch_feed(fetcher, feed, window_start, max_pages)
                rep.messages.extend(msgs)
                return items

            collected += _run_method(rep, _feed)

    # ---- Method B: configured sitemaps
    rep = report.methods.setdefault("sitemap", MethodReport("XML sitemaps (B)", bool(methods.get("sitemap"))))
    if rep.enabled and disc.get("sitemaps"):
        progress("Reading configured sitemaps")

        def _sitemaps():
            items, msgs = discover_from_sitemaps(fetcher, list(disc.get("sitemaps")), window_start, **sm_kwargs)
            rep.messages.extend(msgs)
            return items

        collected += _run_method(rep, _sitemaps)

    # ---- Method D: seed / competitor domains (feeds + sitemaps auto-discovered)
    rep = report.methods.setdefault("seed_domains", MethodReport("Competitor domains (D)", bool(methods.get("seed_domains"))))
    if rep.enabled:
        for domain in config.get("competitor_domains") or []:
            base = domain_to_base_url(domain)
            if not base:
                continue
            progress(f"Discovering feeds and sitemaps for {domain}")

            def _domain(base=base, domain=domain):
                found: list[DiscoveredItem] = []
                feeds, msgs = discover_feeds(fetcher, base)
                rep.messages.extend(f"{domain}: {m}" for m in msgs)
                for feed in feeds[:2]:
                    items, fmsgs = fetch_feed(fetcher, feed, window_start, max_pages, source_label=f"seed_feed:{feed}")
                    rep.messages.extend(f"{domain}: {m}" for m in fmsgs)
                    found += items
                if methods.get("sitemap", True):
                    sitemaps, smsgs = find_sitemaps(fetcher, base)
                    rep.messages.extend(f"{domain}: {m}" for m in smsgs)
                    items, imsgs = discover_from_sitemaps(fetcher, sitemaps, window_start,
                                                          source_label="seed_sitemap", **sm_kwargs)
                    rep.messages.extend(f"{domain}: {m}" for m in imsgs)
                    # Keep only URLs on (or under) the seed domain.
                    found += [i for i in items if get_domain(i.url).endswith(get_domain(base))]
                return found

            collected += _run_method(rep, _domain)

    report.total_found = len(collected)
    progress(f"Found {len(collected)} URLs; de-duplicating and filtering")

    # ---- Merge duplicates
    merged: dict[str, DiscoveredItem] = {}
    for item in collected:
        key = normalize_url(item.url)
        if not key.startswith("http"):
            continue
        if key in merged:
            merged[key].merge(item)
        else:
            merged[key] = item
    report.unique_found = len(merged)

    titles = [i.title for i in merged.values() if i.title]
    report.suggested_phrases = mine_title_phrases(titles, [q.text for q in report.queries if q.is_user_supplied])

    # ---- Filter
    filter_kw = bool(disc.get("filter_by_keywords", True)) and bool(report.queries)
    reprocess = bool(disc.get("reprocess_seen_urls", False))
    slack = timedelta(hours=24)
    keep: list[DiscoveredItem] = []
    for key, item in merged.items():
        ok, why = is_article_like(item.url)
        if not ok:
            report.rejected.append((item.url, f"not_article_like: {why}"))
            continue
        published = [e.value for e in item.date_evidence if e.kind == "published"]
        modified = [e.value for e in item.date_evidence if e.kind == "modified"]
        if published and max(published) < window_start - slack:
            report.rejected.append((item.url, "published_before_window: per discovery source date"))
            continue
        if not published and modified and max(modified) < window_start:
            report.rejected.append((item.url, "not_modified_in_window: sitemap/feed lastmod is older"))
            continue
        url_date = date_from_url(item.url)
        if url_date and url_date[2]:
            try:
                d = datetime(url_date[0], url_date[1], url_date[2], tzinfo=now.tzinfo)
                if d < window_start - timedelta(hours=48):
                    report.rejected.append((item.url, f"url_date_before_window: {d.date()}"))
                    continue
            except ValueError:
                pass
        if filter_kw:
            if item.user_keyword:
                pass  # found by a keyword query; body is re-checked after extraction
            elif item.title or item.summary:
                matched = match_keywords(f"{item.title} {item.summary}", report.queries)
                if matched:
                    item.user_keyword = " | ".join(dict.fromkeys(q.user_keyword for q in matched))
                    item.discovery_query = item.discovery_query or matched[0].text
                    item.query_origin = item.query_origin or matched[0].origin
                elif item.summary:
                    report.rejected.append((item.url, "no_keyword_match: title/summary"))
                    continue
                # Title-only items without a match are checked against the body after extraction.
            # Items with no title (sitemaps) are checked after extraction.
        if not reprocess and storage is not None and storage.is_seen(key):
            report.rejected.append((item.url, "already_processed: seen in an earlier scan"))
            continue
        keep.append(item)

    # ---- Prioritise: keyword-matched first, then stronger date evidence, then newest.
    def priority(it: DiscoveredItem):
        best_rank = max((RANK.get(SOURCE_RELIABILITY.get(e.source, "weak"), 1)
                         for e in it.date_evidence if e.kind == "published"), default=0)
        newest = max((e.value for e in it.date_evidence), default=window_start)
        return (0 if it.user_keyword else 1, -best_rank, -newest.timestamp())

    keep.sort(key=priority)
    max_urls = int(disc.get("max_urls", 120))
    per_domain = int(disc.get("max_urls_per_domain", 25))
    domain_counts: dict[str, int] = {}
    for item in keep:
        d = item.domain
        if domain_counts.get(d, 0) >= per_domain:
            report.rejected.append((item.url, f"domain_limit: more than {per_domain} URLs from {d}"))
            continue
        if len(report.candidates) >= max_urls:
            report.rejected.append((item.url, f"max_urls_limit: scan limited to {max_urls} URLs"))
            continue
        domain_counts[d] = domain_counts.get(d, 0) + 1
        report.candidates.append(item)
    for m in report.methods.values():
        log.info("%s: enabled=%s ok=%s found=%d", m.name, m.enabled, m.ok, m.found)
    return report


def item_to_dict(item: DiscoveredItem) -> dict:
    return {
        "url": item.url, "title": item.title, "summary": item.summary, "discovery_source": item.discovery_source,
        "discovery_query": item.discovery_query, "query_origin": item.query_origin, "user_keyword": item.user_keyword,
        "publisher_url": item.publisher_url,
        "search_ranks": item.search_ranks,
        "date_evidence": [{"source": e.source, "kind": e.kind, "value": e.value.isoformat(), "raw": e.raw,
                           "tz_assumed": e.tz_assumed, "date_only": e.date_only} for e in item.date_evidence],
    }


def item_from_dict(d: dict) -> DiscoveredItem:
    from .date_verification import DateEvidence

    item = DiscoveredItem(url=d["url"], title=d.get("title", ""), summary=d.get("summary", ""),
                          discovery_source=d.get("discovery_source", ""), discovery_query=d.get("discovery_query", ""),
                          query_origin=d.get("query_origin", ""), user_keyword=d.get("user_keyword", ""),
                          publisher_url=d.get("publisher_url", ""), search_ranks=list(d.get("search_ranks", [])))
    for e in d.get("date_evidence", []):
        item.date_evidence.append(DateEvidence(source=e["source"], kind=e["kind"],
                                               value=datetime.fromisoformat(e["value"]), raw=e.get("raw", ""),
                                               tz_assumed=e.get("tz_assumed", False), date_only=e.get("date_only", False)))
    return item


__all__ = ["run_discovery", "DiscoveryReport", "item_to_dict", "item_from_dict"]
