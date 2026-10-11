"""URL normalization, deduplication and article-likeness heuristics."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "yclid", "igshid",
    "ref", "ref_src", "source", "cmpid", "_hsenc", "_hsmi", "mkt_tok", "spm",
    "ocid", "cid", "s_kwcid", "guccounter", "amp",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "__hs")

# Path segments that indicate a listing / utility page rather than an article.
NON_ARTICLE_SEGMENTS = {
    "tag", "tags", "category", "categories", "author", "authors", "page", "archive",
    "archives", "search", "login", "logout", "signin", "sign-in", "signup", "sign-up",
    "register", "account", "cart", "checkout", "wp-admin", "wp-login.php", "feed",
    "rss", "subscribe", "contact", "about", "privacy", "privacy-policy", "terms",
    "terms-of-service", "careers", "jobs", "pricing", "topics", "topic", "user",
    "users", "profile", "members", "events", "webinars", "newsletter", "sitemap",
    "cookie-policy", "legal", "press", "team", "wp-json", "attachment", "comments",
}
NON_ARTICLE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp3", ".mp4",
    ".mov", ".xml", ".json", ".css", ".js", ".ico", ".txt", ".csv", ".xlsx", ".docx",
)
DATE_IN_PATH = re.compile(r"/(20\d{2})/(0?[1-9]|1[0-2])(?:/(0?[1-9]|[12]\d|3[01]))?/")


def normalize_url(url: str, base: str | None = None) -> str:
    """Canonical form used for deduplication.

    Lowercases scheme/host, removes default ports, fragments, tracking parameters,
    duplicate slashes and trailing slashes (except the root), and sorts the query.
    """
    if not url:
        return ""
    url = url.strip()
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return url
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def get_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url_a: str, url_b: str) -> bool:
    return get_domain(url_a) == get_domain(url_b)


def domain_to_base_url(domain_or_url: str) -> str:
    """Accept 'example.com', 'https://example.com/blog/' etc. and return a URL with scheme."""
    value = domain_or_url.strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parts = urlsplit(value)
    path = parts.path if parts.path not in ("", "/") else "/"
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), path, "", ""))


def is_article_like(url: str) -> tuple[bool, str]:
    """Heuristic check: does this URL look like a single article page?

    Returns (is_article, reason). Conservative: only rejects clear non-article patterns.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, "not http(s)"
    path = parts.path.lower()
    if path in ("", "/"):
        return False, "homepage"
    if path.endswith(NON_ARTICLE_EXTENSIONS):
        return False, "file/media URL"
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        if seg in NON_ARTICLE_SEGMENTS:
            return False, f"non-article section '/{seg}/'"
    if re.search(r"/page/\d+", path) or re.search(r"[?&](page|paged|p)=\d+", "?" + parts.query):
        return False, "pagination"
    if re.fullmatch(r"/(20\d{2})(/\d{1,2}){0,2}", path.rstrip("/")):
        return False, "date archive"
    if parts.query and re.search(r"(^|&)(s|q|search)=", parts.query):
        return False, "search results"
    last = segments[-1] if segments else ""
    if len(segments) == 1 and last in {"blog", "news", "articles", "posts", "insights", "resources"}:
        return False, "blog index"
    # Article slugs usually contain several words or an ID.
    if "-" not in last and "_" not in last and not re.search(r"\d", last) and len(last) < 12:
        return False, "short non-slug path"
    return True, "article-like slug"


def date_from_url(url: str) -> tuple[int, int, int | None] | None:
    m = DATE_IN_PATH.search(urlsplit(url).path + "/")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
