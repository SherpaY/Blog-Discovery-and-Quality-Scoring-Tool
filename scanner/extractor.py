"""Article extraction from HTML: metadata, JSON-LD, dates, main content and links.

Uses Beautiful Soup with transparent heuristics (no paid rendering service).
JavaScript-rendered pages are detected and reported rather than rendered.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag

from .date_verification import DateEvidence, make_evidence
from .urlutils import date_from_url, get_domain

ARTICLE_TYPES = {"article", "blogposting", "newsarticle", "techarticle", "reportagenewsarticle",
                 "analysisnewsarticle", "opinionnewsarticle", "reviewnewsarticle", "review",
                 "howto", "scholarlyarticle", "socialmediaposting", "liveblogposting", "webpage"}
STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form", "button", "input",
              "select", "textarea", "nav", "footer", "aside", "dialog", "canvas", "object", "embed")
BOILERPLATE_TOKENS = {"ad", "ads", "advert", "advertisement", "adsbygoogle", "sponsored", "sponsor",
                      "promo", "promotion", "cookie", "cookies", "consent", "gdpr", "newsletter",
                      "subscribe", "subscription", "signup", "share", "sharing", "social", "related",
                      "recommended", "recommendations", "comments", "comment", "sidebar", "breadcrumb",
                      "breadcrumbs", "menu", "navbar", "nav", "popup", "modal", "banner", "toc-toggle",
                      "pagination", "pager", "author-bio", "authorbox", "tags", "tag-list", "footer",
                      "masthead", "skip-link", "visually-hidden", "sr-only", "paywall", "outbrain", "taboola"}
CONTENT_HINTS = ("articlebody", "article-body", "article__body", "article-content", "post-content",
                 "post-body", "entry-content", "story-body", "blog-post", "post__content", "content-body",
                 "single-content", "rich-text", "prose")
SHARE_HOSTS = ("twitter.com/intent", "x.com/intent", "facebook.com/sharer", "linkedin.com/share",
               "pinterest.com/pin/create", "reddit.com/submit", "api.whatsapp.com", "t.me/share",
               "mailto:", "news.ycombinator.com/submitlink")
BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "figcaption", "td", "dd", "dt")

MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|" \
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
VISIBLE_DATE_RE = re.compile(
    rf"(?P<label>published|posted(?: on)?|last updated|updated(?: on)?|modified|date)?[:\s]*"
    rf"(?P<date>{MONTHS}\.? \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}}|\d{{1,2}}(?:st|nd|rd|th)? {MONTHS}\.?,? \d{{4}}|"
    rf"\d{{4}}-\d{{2}}-\d{{2}})",
    re.I,
)

BOT_CHALLENGE_PATTERNS = (
    "just a moment", "attention required", "verify you are human", "verifying you are human",
    "are you a robot", "checking your browser", "access denied", "pardon our interruption",
    "request unsuccessful", "please enable cookies", "captcha", "bot detection", "unusual traffic",
    "security check", "ddos protection",
)

OTHER_PUBLISHED_META = {"date", "pubdate", "publishdate", "publish-date", "publish_date", "dc.date.issued",
                        "dc.date", "dcterms.created", "dcterms.issued", "sailthru.date", "parsely-pub-date",
                        "citation_publication_date", "publication_date", "article.published", "og:published_time",
                        "datepublished", "cxenseparse:recs:publishtime"}
OTHER_MODIFIED_META = {"last-modified", "dcterms.modified", "article.updated", "lastmod", "datemodified"}


@dataclass
class ArticleData:
    url: str
    final_url: str = ""
    canonical_url: str = ""
    title: str = ""
    meta_description: str = ""
    author: str = ""
    headings: list[tuple[int, str]] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)
    blocks: list[tuple[str, str]] = field(default_factory=list)  # ordered (tag, text)
    body_text: str = ""
    outbound_links: list[str] = field(default_factory=list)
    internal_link_count: int = 0
    jsonld_types: list[str] = field(default_factory=list)
    jsonld_article: dict = field(default_factory=dict)
    date_evidence: list[DateEvidence] = field(default_factory=list)
    code_block_count: int = 0
    ordered_list_count: int = 0
    table_count: int = 0
    image_count: int = 0
    extraction_status: str = "ok"
    extraction_notes: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b\w[\w'’-]*\b", self.body_text))

    @property
    def external_link_count(self) -> int:
        return len(self.outbound_links)


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _flatten_jsonld(node) -> list[dict]:
    out = []
    if isinstance(node, list):
        for n in node:
            out.extend(_flatten_jsonld(n))
    elif isinstance(node, dict):
        out.append(node)
        if "@graph" in node:
            out.extend(_flatten_jsonld(node["@graph"]))
        main = node.get("mainEntity") or node.get("mainEntityOfPage")
        if isinstance(main, (dict, list)):
            out.extend(_flatten_jsonld(main))
    return out


def _types(obj: dict) -> list[str]:
    t = obj.get("@type", [])
    t = t if isinstance(t, list) else [t]
    return [str(x).lower() for x in t]


def parse_jsonld(soup: BeautifulSoup) -> list[dict]:
    objs = []
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = script.string or script.get_text() or ""
        raw = raw.strip().strip(";")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            try:  # common breakage: trailing commas / control characters
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", re.sub(r"[\x00-\x1f]", " ", raw)))
            except ValueError:
                continue
        objs.extend(_flatten_jsonld(data))
    return objs


def _author_name(value) -> str:
    if isinstance(value, list):
        names = [_author_name(v) for v in value]
        return ", ".join(n for n in names if n)
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if isinstance(value, str) and not value.startswith("http"):
        return value.strip()
    return ""


def _meta_map(soup: BeautifulSoup) -> dict[str, str]:
    meta = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or m.get("http-equiv") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if key and content and key not in meta:
            meta[key] = content
    return meta


def _is_boilerplate(el: Tag) -> bool:
    if el.name in ("body", "html", "article", "main"):
        return False
    attrs = " ".join([el.get("id") or "", " ".join(el.get("class") or []), el.get("role") or "",
                      el.get("aria-label") or ""]).lower()
    if not attrs.strip():
        return False
    if any(h in attrs for h in CONTENT_HINTS):
        return False
    tokens = set(re.split(r"[\s_]+", attrs)) | set(re.split(r"[\s_-]+", attrs))
    if tokens & BOILERPLATE_TOKENS:
        return True
    return el.get("role") in ("navigation", "banner", "contentinfo", "complementary") or \
        el.get("aria-hidden") == "true"


def _clean_tree(soup: BeautifulSoup) -> BeautifulSoup:
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()
    # Site-level <header> (not one inside an <article>) is navigation.
    for header in soup.find_all("header"):
        if not header.find_parent("article"):
            header.decompose()
    for el in list(soup.find_all(True)):
        if el.decomposed if hasattr(el, "decomposed") else False:
            continue
        try:
            if _is_boilerplate(el):
                el.decompose()
        except AttributeError:
            continue
    return soup


def _block_words(el: Tag) -> int:
    return sum(len(_text(p).split()) for p in el.find_all("p"))


def _select_content_root(soup: BeautifulSoup) -> tuple[Tag, str]:
    candidates: list[tuple[Tag, str]] = []
    for sel, label in (("[itemprop=articleBody]", "itemprop=articleBody"), ("article", "<article>"),
                       ("[role=main]", "role=main"), ("main", "<main>")):
        for el in soup.select(sel):
            candidates.append((el, label))
    for hint in CONTENT_HINTS:
        for el in soup.find_all(class_=re.compile(rf"(^|\s){re.escape(hint)}($|\s)", re.I)):
            candidates.append((el, f".{hint}"))
    scored = [(c, label, _block_words(c)) for c, label in candidates]
    scored = [s for s in scored if s[2] >= 30]
    if scored:
        best = max(s[2] for s in scored)
        # Prefer the most specific (innermost) semantic element holding most of the text.
        good = [s for s in scored if s[2] >= 0.7 * best]
        good.sort(key=lambda s: (len(list(s[0].parents)), s[2]), reverse=True)
        return good[0][0], good[0][1]
    # Fallback: densest container of paragraphs.
    best_el, best_words = None, 0
    for el in soup.find_all(["div", "section", "td"]):
        words = sum(len(_text(p).split()) for p in el.find_all("p", recursive=False))
        if words > best_words:
            best_el, best_words = el, words
    if best_el is not None and best_words >= 30:
        return best_el, "paragraph-density fallback"
    return (soup.body or soup), "whole <body> (no content container found)"


def _collect_dates(soup: BeautifulSoup, meta: dict, jsonld_objs: list[dict], article_obj: dict,
                   root: Tag | None, url: str) -> list[DateEvidence]:
    ev: list[DateEvidence] = []

    def add(source, raw, kind=None):
        if raw:
            e = make_evidence(source, raw, kind)
            if e:
                ev.append(e)

    sources = [article_obj] if article_obj else [o for o in jsonld_objs if "datePublished" in o]
    for obj in sources[:1]:
        add("jsonld_datePublished", obj.get("datePublished"), "published")
        add("jsonld_dateModified", obj.get("dateModified"), "modified")
    add("meta_article_published_time", meta.get("article:published_time"), "published")
    add("meta_article_modified_time", meta.get("article:modified_time") or meta.get("og:updated_time"), "modified")
    for key in OTHER_PUBLISHED_META:
        if key in meta:
            add("meta_other_published", meta[key], "published")
            break
    for key in OTHER_MODIFIED_META:
        if key in meta:
            add("meta_other_modified", meta[key], "modified")
            break
    for el in soup.find_all(attrs={"itemprop": re.compile(r"^date(Published|Modified)$")}):
        if el.name == "meta":
            continue
        raw = el.get("content") or el.get("datetime") or _text(el)
        if el["itemprop"] == "datePublished":
            add("microdata_datePublished", raw, "published")
        else:
            add("microdata_dateModified", raw, "modified")

    scope = root or soup
    time_tags = scope.find_all("time", attrs={"datetime": True}) or soup.find_all("time", attrs={"datetime": True})
    for t in time_tags[:3]:
        hints = " ".join([" ".join(t.get("class") or []), t.get("itemprop") or "",
                          " ".join(t.parent.get("class") or []) if t.parent else "",
                          _text(t.parent)[:60] if t.parent else ""]).lower()
        kind = "modified" if re.search(r"updat|modif", hints) else "published"
        add("time_element" if kind == "published" else "time_element_modified", t["datetime"], kind)
        if kind == "published":
            break

    # Visible text near the top of the article (byline area).
    head_text = " ".join(_text(el) for el in (scope.find_all(class_=re.compile(r"date|byline|meta|posted|time", re.I))[:6]))
    head_text = (head_text + " " + _text(scope)[:400])[:1200]
    for m in VISIBLE_DATE_RE.finditer(head_text):
        label = (m.group("label") or "").lower()
        kind = "modified" if ("updat" in label or "modif" in label) else "published"
        add("visible_text" if kind == "published" else "visible_text_modified", m.group("date"), kind)
        if kind == "published":
            break

    url_date = date_from_url(url)
    if url_date and url_date[2]:
        add("url_path", f"{url_date[0]:04d}-{url_date[1]:02d}-{url_date[2]:02d}", "published")
    return ev


def extract_article(html: str | bytes, url: str, final_url: str | None = None) -> ArticleData:
    """Extract everything the scorer and date verifier need. Never raises on bad HTML."""
    final_url = final_url or url
    data = ArticleData(url=url, final_url=final_url)
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    if not html or "<" not in html:
        data.extraction_status = "failed"
        data.extraction_notes.append("empty or non-HTML response")
        return data
    soup = BeautifulSoup(html, "lxml")
    raw_script_count = len(soup.find_all("script"))
    meta = _meta_map(soup)
    jsonld_objs = parse_jsonld(soup)
    data.jsonld_types = sorted({t for o in jsonld_objs for t in _types(o)})
    article_obj = next((o for o in jsonld_objs if set(_types(o)) & (ARTICLE_TYPES - {"webpage"})), {}) or \
        next((o for o in jsonld_objs if "webpage" in _types(o) and "datePublished" in o), {})
    data.jsonld_article = {k: article_obj.get(k) for k in ("@type", "headline", "datePublished", "dateModified",
                                                           "articleSection", "wordCount") if k in article_obj}

    canonical = soup.find("link", rel=lambda r: r and "canonical" in [x.lower() for x in (r if isinstance(r, list) else [r])])
    if canonical and canonical.get("href"):
        can = urljoin(final_url, canonical["href"].strip())
        if can.startswith(("http://", "https://")):
            data.canonical_url = can
    if not data.canonical_url and meta.get("og:url", "").startswith("http"):
        data.canonical_url = meta["og:url"]

    h1 = soup.find("h1")
    data.title = (str(article_obj.get("headline") or "").strip() or meta.get("og:title", "")
                  or _text(h1) or _text(soup.title))[:300]
    data.meta_description = (meta.get("description") or meta.get("og:description") or "")[:500]
    data.author = (_author_name(article_obj.get("author")) or meta.get("author", "")
                   or (meta.get("article:author", "") if not meta.get("article:author", "").startswith("http") else "")
                   or _text(soup.find(attrs={"rel": "author"})) or _text(soup.find(class_=re.compile(r"author-name|byline__name", re.I))))[:200]

    # Dates from the full document before cleaning (bylines often live in headers).
    pre_root = soup.find("article") or soup.find("main")
    data.date_evidence = _collect_dates(soup, meta, jsonld_objs, article_obj, pre_root, final_url)

    cleaned = _clean_tree(copy.copy(soup))
    root, root_label = _select_content_root(cleaned)
    # If the chosen container is thin but the page has much more paragraph text, use the whole body.
    body_el = cleaned.body or cleaned
    root_words = _block_words(root)
    body_words = _block_words(body_el)
    if root_words < 120 and body_words >= max(150, 2 * root_words) and root is not body_el:
        root, root_label = body_el, f"whole <body> ({root_label} held only {root_words} words)"
    data.extraction_notes.append(f"content root: {root_label}")

    seen_blocks: set[int] = set()
    for el in root.find_all(BLOCK_TAGS):
        if any(id(p) in seen_blocks for p in el.parents):
            continue
        text = _text(el)
        if not text:
            continue
        seen_blocks.add(id(el))
        data.blocks.append((el.name, text))
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            data.headings.append((int(el.name[1]), text[:200]))
        elif el.name == "li":
            data.list_items.append(text)
        elif el.name in ("p", "blockquote"):
            data.paragraphs.append(text)
    data.body_text = "\n".join(t for tag, t in data.blocks if tag not in ("h1",))

    page_domain = get_domain(final_url)
    links = []
    for a in root.find_all("a", href=True):
        href = urljoin(final_url, a["href"].strip())
        if not href.startswith(("http://", "https://")) or any(s in href for s in SHARE_HOSTS):
            continue
        dom = get_domain(href)
        if dom == page_domain or dom.endswith("." + page_domain) or page_domain.endswith("." + dom):
            data.internal_link_count += 1
        else:
            links.append(href.split("#")[0])
    data.outbound_links = list(dict.fromkeys(links))
    data.code_block_count = len(root.find_all("pre"))
    data.ordered_list_count = len(root.find_all("ol"))
    data.table_count = len(root.find_all("table"))
    data.image_count = len(root.find_all("img"))

    # Many publishers embed the full text in JSON-LD; use it when the HTML yielded little.
    jl_body = article_obj.get("articleBody") if isinstance(article_obj.get("articleBody"), str) else ""
    if jl_body and len(jl_body.split()) > max(80, 1.5 * data.word_count):
        paras = [re.sub(r"\s+", " ", p_).strip() for p_ in re.split(r"\n\s*\n|\r\n|\n", jl_body)]
        paras = [p_ for p_ in paras if p_]
        if len(paras) <= 1:  # no line breaks: split into ~4-sentence paragraphs
            sents = re.split(r"(?<=[.!?])\s+", jl_body.strip())
            paras = [" ".join(sents[i:i + 4]) for i in range(0, len(sents), 4)]
        heading_blocks = [b for b in data.blocks if b[0] in ("h2", "h3", "h4", "h5", "h6")]
        data.paragraphs = paras
        data.blocks = heading_blocks + [("p", p_) for p_ in paras]
        data.body_text = "\n".join(paras)
        data.extraction_notes.append("body text taken from JSON-LD articleBody (HTML text was thin)")

    words = data.word_count
    page_text = _text(soup.body)[:3000].lower() if soup.body else ""
    title_text = (_text(soup.title) or "").lower()
    spa_root = soup.find(id=re.compile(r"^(root|app|__next|___gatsby|__nuxt)$"))
    challenge = next((p_ for p_ in BOT_CHALLENGE_PATTERNS if p_ in title_text or p_ in page_text[:600]), None)
    if words < 150 and challenge:
        data.extraction_status = "blocked_bot_protection"
        data.extraction_notes.append(f"site served a bot-protection / challenge page ('{challenge}'); "
                                     "not bypassed - open the URL in a browser to review it")
    elif words < 120 and (raw_script_count >= 8 or spa_root is not None or "enable javascript" in page_text
                          or "javascript is required" in page_text):
        data.extraction_status = "js_rendered_suspected"
        data.extraction_notes.append(f"only {words} words in the server-rendered HTML; the page probably loads its "
                                     "text with JavaScript or blocked automated access (not rendered or bypassed)")
    elif words < 50:
        data.extraction_status = "thin_content"
        data.extraction_notes.append("very little text extracted")
    return data


def is_html_response(content_type: str, content: bytes) -> bool:
    if "html" in content_type:
        return True
    head = content[:600].lstrip().lower()
    return head.startswith(b"<!doctype html") or b"<html" in head
