"""Shared test helpers. No test in the main suite touches the network."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from scanner.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        if "Content-Length" not in self.headers:
            self.headers["Content-Length"] = str(len(self._body))

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """Maps URLs to canned responses. Values: (status, body, headers) or an Exception instance."""

    def __init__(self, routes: dict | None = None, default_status: int = 404):
        self.routes = dict(routes or {})
        self.default_status = default_status
        self.calls: list[str] = []

    def add(self, url, body=b"", status=200, headers=None, content_type=None):
        headers = dict(headers or {})
        if content_type:
            headers["Content-Type"] = content_type
        self.routes[url] = (status, body, headers)

    def get(self, url, **kwargs):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(self.default_status, b"not found", {"Content-Type": "text/html"})
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(url)
        status, body, headers = route
        return FakeResponse(status, body, headers)


def public_resolver(host: str):
    """Pretend every hostname resolves to a public address, except a few test hosts."""
    special = {
        "internal.example.com": ["10.0.0.5"],
        "metadata.test": ["169.254.169.254"],
        "loopback.test": ["127.0.0.1"],
        "mixed.test": ["93.184.216.34", "192.168.1.10"],
    }
    if host in special:
        return special[host]
    if host == "nxdomain.test":
        raise OSError("Name or service not known")
    return ["93.184.216.34"]


@pytest.fixture
def base_config(tmp_path):
    cfg = copy.deepcopy(load_config(user_path=tmp_path / "none.yaml"))
    cfg["paths"] = {"data_dir": str(tmp_path / "data"), "output_dir": str(tmp_path / "output"),
                    "log_dir": str(tmp_path / "logs")}
    cfg["http"]["min_delay_seconds"] = 0
    cfg["http"]["max_retries"] = 1
    cfg["discovery"]["search_delay_seconds"] = 0
    return cfg


@pytest.fixture
def fetcher_factory():
    from scanner.http_client import Fetcher

    def make(session, **overrides):
        cfg = {"min_delay_seconds": 0, "timeout_seconds": 1, "max_retries": 1, "cache_ttl_hours": 0}
        cfg.update(overrides)
        return Fetcher(cfg, session=session, resolver=public_resolver, sleep=lambda s: None)

    return make


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
