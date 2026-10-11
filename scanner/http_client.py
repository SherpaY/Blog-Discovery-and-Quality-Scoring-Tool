"""Polite, safe HTTP fetching.

* descriptive User-Agent, timeouts, response size limits
* robots.txt checks (RFC 9309 semantics) and Crawl-delay
* per-domain rate limiting
* manual redirect handling with an SSRF check on every hop
* bounded retries (never infinite) and an optional on-disk cache
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .safety import Resolver, UnsafeURLError, check_url

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    content: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str | None = None
    from_cache: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or self.headers.get("Content-Type") or "").lower()

    @property
    def text(self) -> str:
        enc = "utf-8"
        ct = self.content_type
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class Cache(Protocol):
    def get(self, url: str, max_age_seconds: float) -> FetchResult | None: ...
    def put(self, result: FetchResult) -> None: ...


class StopRequested(Exception):
    """Raised inside long loops when the user asked the scan to stop."""


class Fetcher:
    def __init__(
        self,
        http_config: dict | None = None,
        session: requests.Session | None = None,
        resolver: Resolver | None = None,
        cache: Cache | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        stop_check: Callable[[], bool] | None = None,
    ):
        cfg = http_config or {}
        self.user_agent = cfg.get("user_agent", "BlogQualityScanner/1.0")
        self.timeout = float(cfg.get("timeout_seconds", 15))
        self.max_retries = max(0, int(cfg.get("max_retries", 1)))
        self.min_delay = float(cfg.get("min_delay_seconds", 2.0))
        self.max_crawl_delay = float(cfg.get("max_crawl_delay_seconds", 30))
        self.max_bytes = int(cfg.get("max_page_bytes", 3_000_000))
        self.max_redirects = int(cfg.get("max_redirects", 5))
        self.respect_robots = bool(cfg.get("respect_robots", True))
        self.cache_ttl = float(cfg.get("cache_ttl_hours", 6)) * 3600
        self.session = session or requests.Session()
        self.resolver = resolver
        self.cache = cache
        self._sleep = sleep
        self._clock = clock
        self._stop_check = stop_check or (lambda: False)
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | str] = {}
        self._lock = threading.Lock()
        self.request_count = 0

    # ------------------------------------------------------------------ robots
    def _origin(self, url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def robots_for(self, url: str) -> RobotFileParser | str:
        """Return a parser, or the string 'allow_all' / 'disallow_all'."""
        origin = self._origin(url)
        if origin in self._robots:
            return self._robots[origin]
        robots_url = origin + "/robots.txt"
        res = self._fetch_raw(robots_url, max_bytes=500_000, use_cache=True)
        if res.status is not None and 200 <= res.status < 300:
            parser = RobotFileParser()
            parser.parse(res.text.splitlines())
            outcome: RobotFileParser | str = parser
        elif res.status is not None and 400 <= res.status < 500:
            outcome = "allow_all"  # RFC 9309: robots.txt unavailable -> crawling allowed
        else:
            outcome = "disallow_all"  # unreachable / 5xx -> assume complete disallow
            log.info("robots.txt unreachable for %s (%s); treating as disallow", origin, res.error or res.status)
        self._robots[origin] = outcome
        return outcome

    def robots_text(self, url: str) -> str:
        res = self._fetch_raw(self._origin(url) + "/robots.txt", max_bytes=500_000, use_cache=True)
        return res.text if res.ok else ""

    def is_allowed(self, url: str) -> tuple[bool, str]:
        if not self.respect_robots:
            return True, "robots.txt checks disabled"
        rules = self.robots_for(url)
        if rules == "allow_all":
            return True, "no robots.txt"
        if rules == "disallow_all":
            return False, "robots.txt unreachable (treated as disallow)"
        assert isinstance(rules, RobotFileParser)
        if rules.can_fetch(self.user_agent, url):
            return True, "allowed by robots.txt"
        return False, "disallowed by robots.txt"

    def crawl_delay(self, url: str) -> float:
        rules = self._robots.get(self._origin(url))
        delay = None
        if isinstance(rules, RobotFileParser):
            try:
                delay = rules.crawl_delay(self.user_agent)
            except Exception:  # noqa: BLE001 - robotparser edge cases
                delay = None
        return max(self.min_delay, float(delay or 0))

    # ------------------------------------------------------------------ fetching
    def fetch(self, url: str, max_bytes: int | None = None, check_robots: bool = True,
              use_cache: bool = True) -> FetchResult:
        """Fetch a URL safely. Never raises for network problems; inspect .error instead."""
        try:
            check_url(url, self.resolver)
        except UnsafeURLError as exc:
            kind = "dns_error" if str(exc).startswith("DNS") else "unsafe_url"
            return FetchResult(url=url, final_url=url, error=f"{kind}: {exc}")
        if check_robots:
            allowed, reason = self.is_allowed(url)
            if not allowed:
                return FetchResult(url=url, final_url=url, error=f"robots: {reason}")
            if self.crawl_delay(url) > self.max_crawl_delay:
                return FetchResult(url=url, final_url=url,
                                   error=f"robots: crawl-delay {self.crawl_delay(url):.0f}s exceeds limit")
        return self._fetch_raw(url, max_bytes=max_bytes, use_cache=use_cache, check_robots=check_robots)

    def _wait_for_slot(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = self.crawl_delay(url)
        with self._lock:
            last = self._last_request.get(host)
            now = self._clock()
            wait = 0.0 if last is None else max(0.0, last + delay - now)
            self._last_request[host] = now + wait
        if wait > 0:
            self._sleep(wait)

    def _fetch_raw(self, url: str, max_bytes: int | None = None, use_cache: bool = True,
                   check_robots: bool = False) -> FetchResult:
        if self._stop_check():
            raise StopRequested()
        limit = max_bytes or self.max_bytes
        if use_cache and self.cache is not None:
            cached = self.cache.get(url, self.cache_ttl)
            if cached is not None:
                cached.from_cache = True
                return cached

        current = url
        redirects: list[str] = []
        for _hop in range(self.max_redirects + 1):
            try:
                check_url(current, self.resolver)
            except UnsafeURLError as exc:
                return FetchResult(url=url, final_url=current, error=f"unsafe_redirect: {exc}", redirects=redirects)
            if check_robots and redirects:
                allowed, reason = self.is_allowed(current)
                if not allowed:
                    return FetchResult(url=url, final_url=current, error=f"robots: {reason} (after redirect)",
                                       redirects=redirects)
            result = self._request_with_retries(current, limit)
            result.url = url
            result.redirects = redirects
            if result.error or result.status is None:
                return result
            if 300 <= result.status < 400 and result.headers.get("location"):
                nxt = urljoin(current, result.headers["location"])
                redirects.append(current)
                current = nxt
                continue
            result.final_url = current
            if use_cache and self.cache is not None and result.ok:
                self.cache.put(result)
            return result
        return FetchResult(url=url, final_url=current, error="too_many_redirects", redirects=redirects)

    def _request_with_retries(self, url: str, limit: int) -> FetchResult:
        attempt = 0
        while True:
            self._wait_for_slot(url)
            if self._stop_check():
                raise StopRequested()
            self.request_count += 1
            try:
                resp = self.session.get(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                  "application/rss+xml,application/atom+xml,*/*;q=0.5",
                        "Accept-Language": "en;q=0.9,*;q=0.5",
                    },
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout:
                err = "timeout"
            except requests.ConnectionError as exc:
                err = f"connection_error: {str(exc)[:160]}"
            except requests.RequestException as exc:
                err = f"request_error: {str(exc)[:160]}"
            else:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                status = resp.status_code
                if status in RETRYABLE_STATUS and attempt < self.max_retries:
                    retry_after = headers.get("retry-after", "")
                    wait = float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1)
                    resp.close()
                    if wait > 60:
                        return FetchResult(url=url, final_url=url, status=status, headers=headers,
                                           error=f"http_{status}: retry-after {wait:.0f}s too long")
                    attempt += 1
                    self._sleep(wait)
                    continue
                try:
                    body, truncated = self._read_limited(resp, limit)
                except requests.RequestException as exc:
                    return FetchResult(url=url, final_url=url, status=status, headers=headers,
                                       error=f"read_error: {str(exc)[:160]}")
                finally:
                    resp.close()
                res = FetchResult(url=url, final_url=url, status=status, content=body, headers=headers)
                if truncated:
                    res.error = f"too_large: response exceeds {limit} bytes"
                elif status >= 400:
                    res.error = f"http_{status}"
                return res
            if attempt < self.max_retries:
                attempt += 1
                self._sleep(2.0 * attempt)
                continue
            return FetchResult(url=url, final_url=url, error=err)

    @staticmethod
    def _read_limited(resp, limit: int) -> tuple[bytes, bool]:
        declared = resp.headers.get("Content-Length") or resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return b"", True
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False
