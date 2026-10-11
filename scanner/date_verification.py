"""Publication-date verification.

Collects date evidence from several sources, keeps published and modified dates
strictly separate, cross-checks them and produces a verdict with a confidence
level and a human-readable evidence log.

Never substitutes a modified date for a publication date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dateutil import parser as dateparser
from dateutil import tz

# How much each source is trusted for a *publication* date.
SOURCE_RELIABILITY = {
    "jsonld_datePublished": "strong",
    "meta_article_published_time": "strong",
    "feed_published": "strong",
    "meta_other_published": "medium",
    "microdata_datePublished": "medium",
    "time_element": "medium",
    "news_sitemap_publication_date": "medium",
    "search_result_date": "medium",
    "visible_text": "weak",
    "url_path": "weak",
    # Modification-only sources (never used as publication date):
    "jsonld_dateModified": "modified",
    "meta_article_modified_time": "modified",
    "feed_updated": "modified",
    "sitemap_lastmod": "modified",
    "http_last_modified": "modified",
    "meta_other_modified": "modified",
    "microdata_dateModified": "modified",
    "time_element_modified": "modified",
    "visible_text_modified": "modified",
}
RANK = {"strong": 3, "medium": 2, "weak": 1, "modified": 0}
CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

DATE_STATUS_CONFIRMED = "confirmed_recent"
DATE_STATUS_OUTSIDE = "outside_window"
DATE_STATUS_UNCERTAIN = "uncertain"


@dataclass
class DateEvidence:
    source: str
    kind: str  # "published" | "modified"
    value: datetime  # always timezone-aware UTC
    raw: str = ""
    tz_assumed: bool = False  # True if the raw value had no timezone (UTC assumed)
    date_only: bool = False  # True if the raw value had no time component

    def describe(self) -> str:
        notes = []
        if self.tz_assumed:
            notes.append("no timezone, UTC assumed")
        if self.date_only:
            notes.append("date only")
        extra = f" ({', '.join(notes)})" if notes else ""
        return f"{self.kind}:{self.source}={self.value.strftime('%Y-%m-%dT%H:%MZ')}{extra}"


@dataclass
class DateVerdict:
    publication_date: datetime | None
    modification_date: datetime | None
    confidence: str  # high | medium | low | none
    status: str  # confirmed_recent | outside_window | uncertain
    flags: list[str] = field(default_factory=list)
    evidence: list[DateEvidence] = field(default_factory=list)
    primary_source: str = ""

    @property
    def evidence_text(self) -> str:
        parts = [e.describe() for e in self.evidence]
        if self.primary_source:
            parts.insert(0, f"primary={self.primary_source}")
        if self.flags:
            parts.append("flags=" + ",".join(self.flags))
        return " | ".join(parts)


_DATE_ONLY_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")
_HAS_TIME_RE = re.compile(r"\d{1,2}:\d{2}")


def parse_date(raw, default_tz=timezone.utc) -> tuple[datetime, bool, bool] | None:
    """Parse many date formats into an aware UTC datetime.

    Returns (datetime_utc, tz_assumed, date_only) or None if unparseable / implausible.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
        date_only = False
    else:
        text = str(raw).strip()
        if not text or len(text) > 80:
            return None
        date_only = bool(_DATE_ONLY_RE.match(text)) or not _HAS_TIME_RE.search(text)
        try:
            dt = dateparser.parse(text, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            try:
                dt = dateparser.parse(text, fuzzy=True)
            except (ValueError, OverflowError, TypeError):
                return None
        if dt is None:
            return None
    tz_assumed = dt.tzinfo is None
    if tz_assumed:
        dt = dt.replace(tzinfo=default_tz)
    dt = dt.astimezone(timezone.utc)
    if dt.year < 1995 or dt.year > 2100:
        return None
    return dt, tz_assumed, date_only


def make_evidence(source: str, raw, kind: str | None = None) -> DateEvidence | None:
    parsed = parse_date(raw)
    if not parsed:
        return None
    dt, tz_assumed, date_only = parsed
    if kind is None:
        kind = "modified" if SOURCE_RELIABILITY.get(source) == "modified" else "published"
    return DateEvidence(source=source, kind=kind, value=dt, raw=str(raw)[:80],
                        tz_assumed=tz_assumed, date_only=date_only)


def to_local(dt: datetime | None, tz_name: str | None = None) -> datetime | None:
    """Convert a UTC datetime for display in the user's local (or named) timezone."""
    if dt is None:
        return None
    target = tz.gettz(tz_name) if tz_name else tz.tzlocal()
    return dt.astimezone(target)


def _tolerance(a: DateEvidence, b: DateEvidence, base_hours: float) -> timedelta:
    # Date-only values (and assumed timezones) can legitimately differ by up to a day.
    extra = 24 if (a.date_only or b.date_only or a.tz_assumed or b.tz_assumed) else 0
    return timedelta(hours=base_hours + extra)


def verify_dates(
    evidence: list[DateEvidence],
    now: datetime,
    window_hours: float,
    min_confidence: str = "medium",
    agreement_tolerance_hours: float = 36,
) -> DateVerdict:
    """Decide the publication date, its confidence and whether it falls in the window."""
    now = now.astimezone(timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    flags: list[str] = []

    # Deduplicate identical source/value pairs.
    seen, unique = set(), []
    for ev in evidence:
        key = (ev.source, ev.kind, ev.value)
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    evidence = unique

    published = [e for e in evidence if e.kind == "published"]
    modified = [e for e in evidence if e.kind == "modified"]

    future_limit = now + timedelta(hours=2)
    future = [e for e in published if e.value > future_limit]
    if future:
        flags.append("future_publication_date")
        published = [e for e in published if e.value <= future_limit]

    modification_date = max((e.value for e in modified if e.value <= future_limit), default=None)

    if not published:
        if modified:
            flags.append("modification_date_only")
            if modification_date and modification_date >= window_start:
                flags.append("recently_modified_publication_unknown")
        else:
            flags.append("no_date_found")
        return DateVerdict(None, modification_date, "none", DATE_STATUS_UNCERTAIN, flags, evidence)

    # Primary = most reliable source; ties broken by preferring values with a time component.
    published.sort(key=lambda e: (RANK.get(SOURCE_RELIABILITY.get(e.source, "weak"), 1), not e.date_only),
                   reverse=True)
    primary = published[0]
    primary_rank = SOURCE_RELIABILITY.get(primary.source, "weak")

    agreeing = [primary]
    conflicting = []
    for other in published[1:]:
        if abs(other.value - primary.value) <= _tolerance(primary, other, agreement_tolerance_hours):
            agreeing.append(other)
        else:
            conflicting.append(other)

    independent_agreeing = {e.source.split("_")[0] for e in agreeing}
    strong_conflicts = [c for c in conflicting if RANK.get(SOURCE_RELIABILITY.get(c.source, "weak"), 1) >= 2]

    if primary_rank == "strong" and len(independent_agreeing) >= 2:
        confidence = "high"
    elif primary_rank == "strong":
        confidence = "medium"
    elif primary_rank == "medium" and len(independent_agreeing) >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    if strong_conflicts:
        flags.append("conflicting_publication_dates")
        confidence = "low"
    elif conflicting:
        flags.append("minor_date_disagreement")
        if confidence == "high":
            confidence = "medium"

    if primary.tz_assumed and not primary.date_only:
        flags.append("timezone_missing")
    if primary.date_only:
        flags.append("date_without_time")

    if modification_date and modification_date < primary.value - timedelta(hours=24):
        flags.append("modified_before_published")

    publication_date = primary.value

    # Old article that was updated recently: never treated as newly published.
    if publication_date < window_start and modification_date and modification_date >= window_start:
        flags.append("old_article_recently_updated")

    # Window check. Date-only values near the boundary are treated as uncertain.
    boundary_slack = timedelta(hours=24) if primary.date_only else timedelta(0)
    if publication_date >= window_start:
        in_window = True
    elif publication_date >= window_start - boundary_slack:
        in_window = None  # straddles the boundary
        flags.append("near_window_boundary")
    else:
        in_window = False

    if in_window is False:
        status = DATE_STATUS_OUTSIDE
    elif in_window is None or CONFIDENCE_ORDER[confidence] < CONFIDENCE_ORDER.get(min_confidence, 2):
        status = DATE_STATUS_UNCERTAIN
        if in_window:
            flags.append("low_confidence_date")
    else:
        status = DATE_STATUS_CONFIRMED

    return DateVerdict(publication_date, modification_date, confidence, status, flags, evidence, primary.source)
