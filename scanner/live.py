"""Time-limited live search for the hosted dashboard (Vercel function api/scan.py).

A visitor types keywords, the function runs the normal scanner with a hard time
budget, and returns results in the same JSON format as published scans.
Nothing is stored between requests (Vercel has no persistent disk); each search
uses a fresh temporary database.
"""

from __future__ import annotations

import copy
import hmac
import os
import re
import shutil
import tempfile
import time
from datetime import datetime

from .config import load_config
from .dashboard import scan_payload
from .exporter import ALL_COLUMNS
from .pipeline import ScanRunner
from .search_discovery import ALL_SOURCES
from .storage import Storage

WINDOWS = (24, 48, 72, 168)
MAX_KEYWORDS = 5
MAX_DOMAINS = 3
MAX_URLS_LIMIT = 30
DEFAULT_BUDGET_SECONDS = 240   # Vercel Hobby allows 300 s per request; keep a safety margin
DISCOVERY_SHARE = 0.45         # at most this share of the budget is spent finding URLs
DEFAULT_LIVE_SOURCES = ("gdelt_doc", "bing_news_rss", "medium_tag_feed", "devto_tag_feed", "hn_algolia")


class LiveSearchError(ValueError):
    """Invalid request - message is safe to show to the user."""


def _clean_list(value, limit: int, max_len: int, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[\n,]", value)
    if not isinstance(value, list):
        raise LiveSearchError(f"{label} must be a list")
    items = []
    for v in value:
        v = re.sub(r"\s+", " ", str(v)).strip()
        if not v:
            continue
        if len(v) > max_len:
            raise LiveSearchError(f"{label} entries must be at most {max_len} characters")
        items.append(v)
    items = list(dict.fromkeys(items))
    if len(items) > limit:
        raise LiveSearchError(f"at most {limit} {label} per search")
    return items


def check_access_code(provided: str | None) -> bool:
    """If SCAN_ACCESS_CODE is set (Vercel env var), require it. Otherwise the search is open."""
    expected = os.environ.get("SCAN_ACCESS_CODE", "")
    if not expected:
        return True
    return hmac.compare_digest(str(provided or "").encode(), expected.encode())


def build_live_config(params: dict, base: dict | None = None) -> dict:
    keywords = _clean_list(params.get("keywords"), MAX_KEYWORDS, 80, "keywords")
    if not keywords:
        raise LiveSearchError("enter at least one keyword")
    domains = _clean_list(params.get("competitor_domains"), MAX_DOMAINS, 200, "competitor domains")
    try:
        window = int(params.get("window_hours") or 168)
        max_urls = int(params.get("max_urls") or 20)
    except (TypeError, ValueError) as exc:
        raise LiveSearchError("window_hours and max_urls must be numbers") from exc
    if window not in WINDOWS:
        raise LiveSearchError(f"window_hours must be one of {WINDOWS}")
    max_urls = max(5, min(MAX_URLS_LIMIT, max_urls))

    cfg = copy.deepcopy(base if base is not None else load_config(user_path="/nonexistent/user_config.yaml"))
    cfg["keywords"] = keywords
    cfg["competitor_domains"] = domains
    disc = cfg["discovery"]
    disc.update({
        "window_hours": window,
        "max_urls": max_urls,
        "max_urls_per_domain": max(5, max_urls // 2),
        "feeds": [],
        "sitemaps": [],
        "filter_by_keywords": True,
        "reprocess_seen_urls": True,
        "max_search_queries": 3,          # user keywords first, then shorter phrases
        "search_delay_seconds": 2,
        "max_feed_pages": 1,
        "max_sitemaps_per_domain": 3,
        "methods": {"search": True, "rss": False, "sitemap": bool(domains), "seed_domains": bool(domains)},
        # Same sources as switched on in config.yaml (falls back to a sensible default set).
        "search_sources": {name: bool((disc.get("search_sources") or {}).get(name, name in DEFAULT_LIVE_SOURCES))
                           for name in ALL_SOURCES},
    })
    cfg["topics"]["max_generated_queries"] = 4
    cfg["http"]["timeout_seconds"] = min(float(cfg["http"].get("timeout_seconds", 15)), 10)
    cfg["http"]["max_retries"] = 0
    cfg["ollama"] = {"enabled": False}
    return cfg


def run_live_search(params: dict, *, session=None, resolver=None, now: datetime | None = None,
                    budget_seconds: float = DEFAULT_BUDGET_SECONDS, base_config: dict | None = None) -> dict:
    """Run one time-limited scan and return the dashboard payload (never touches the project data dir)."""
    cfg = build_live_config(params, base_config)
    workdir = tempfile.mkdtemp(prefix="live_scan_")
    cfg["paths"] = {"data_dir": f"{workdir}/data", "output_dir": f"{workdir}/output", "log_dir": f"{workdir}/logs"}
    start = time.monotonic()
    try:
        runner = ScanRunner(cfg, session=session, resolver=resolver, now=now,
                            deadline=start + budget_seconds,
                            discovery_deadline=start + budget_seconds * DISCOVERY_SHARE)
        result = runner.run(trigger="live")
        storage = Storage(f"{workdir}/data/scanner.db")
        try:
            _, payload = scan_payload(storage, storage.get_scan(result["scan_id"]))
        finally:
            storage.close()
        payload["file"] = None
        payload["columns"] = ALL_COLUMNS
        payload["live"] = True
        payload["partial"] = result.get("state") == "stopped" or bool(result.get("time_limited"))
        payload["duration_seconds"] = round(time.monotonic() - start, 1)
        return payload
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
