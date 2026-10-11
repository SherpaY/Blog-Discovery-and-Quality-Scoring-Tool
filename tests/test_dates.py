from datetime import datetime, timezone

from scanner.date_verification import (DATE_STATUS_CONFIRMED, DATE_STATUS_OUTSIDE, DATE_STATUS_UNCERTAIN,
                                       make_evidence, parse_date, to_local, verify_dates)

from .conftest import NOW


def ev(source, raw, kind=None):
    e = make_evidence(source, raw, kind)
    assert e is not None, raw
    return e


def test_parse_formats_and_convert_to_utc():
    dt, assumed, date_only = parse_date("Fri, 09 Oct 2026 08:15:00 +0200")
    assert dt == datetime(2026, 10, 9, 6, 15, tzinfo=timezone.utc) and not assumed and not date_only
    dt, assumed, date_only = parse_date("2026-10-10T15:30:00-04:00")
    assert dt.hour == 19 and dt.tzinfo == timezone.utc
    dt, assumed, date_only = parse_date("2026-10-09")
    assert assumed and date_only
    dt, assumed, _ = parse_date("October 9, 2026 10:00")
    assert assumed and dt.day == 9
    assert parse_date("not a date") is None
    assert parse_date("1890-01-01") is None  # implausible


def test_local_display_conversion():
    dt = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    ny = to_local(dt, "America/New_York")
    assert ny.hour == 8 and ny.utcoffset().total_seconds() == -4 * 3600
    assert to_local(dt, "Asia/Kolkata").strftime("%H:%M") == "17:30"


def test_two_agreeing_strong_sources_high_confidence():
    v = verify_dates([ev("jsonld_datePublished", "2026-10-09T08:15:00Z"),
                      ev("meta_article_published_time", "2026-10-09T08:15:00+00:00"),
                      ev("jsonld_dateModified", "2026-10-09T10:00:00Z")], NOW, 168)
    assert v.status == DATE_STATUS_CONFIRMED and v.confidence == "high"
    assert v.publication_date.isoformat() == "2026-10-09T08:15:00+00:00"
    assert v.modification_date.isoformat() == "2026-10-09T10:00:00+00:00"
    assert "primary=jsonld_datePublished" in v.evidence_text


def test_single_strong_source_medium_confidence_still_confirmed():
    v = verify_dates([ev("jsonld_datePublished", "2026-10-10T06:00:00Z")], NOW, 168)
    assert v.confidence == "medium" and v.status == DATE_STATUS_CONFIRMED
    v2 = verify_dates([ev("jsonld_datePublished", "2026-10-10T06:00:00Z")], NOW, 168, min_confidence="high")
    assert v2.status == DATE_STATUS_UNCERTAIN and "low_confidence_date" in v2.flags


def test_conflicting_publication_dates_go_to_review():
    v = verify_dates([ev("jsonld_datePublished", "2026-10-08T09:00:00Z"),
                      ev("meta_article_published_time", "2026-06-01T09:00:00Z")], NOW, 168)
    assert "conflicting_publication_dates" in v.flags
    assert v.confidence == "low" and v.status == DATE_STATUS_UNCERTAIN


def test_modified_date_never_substituted_for_publication():
    v = verify_dates([ev("meta_article_modified_time", "2026-10-10T12:00:00Z"),
                      ev("sitemap_lastmod", "2026-10-10T12:00:00Z")], NOW, 168)
    assert v.publication_date is None
    assert v.modification_date is not None
    assert v.status == DATE_STATUS_UNCERTAIN
    assert "modification_date_only" in v.flags and "recently_modified_publication_unknown" in v.flags


def test_old_article_recently_updated_is_not_recent():
    v = verify_dates([ev("jsonld_datePublished", "2023-02-14T10:00:00Z"),
                      ev("jsonld_dateModified", "2026-10-10T09:00:00Z")], NOW, 168)
    assert v.status == DATE_STATUS_OUTSIDE and "old_article_recently_updated" in v.flags


def test_window_sizes():
    e = [ev("jsonld_datePublished", "2026-10-09T08:00:00Z"), ev("feed_published", "2026-10-09T08:00:00Z")]
    assert verify_dates(e, NOW, 24).status == DATE_STATUS_OUTSIDE
    assert verify_dates(e, NOW, 72).status == DATE_STATUS_CONFIRMED
    assert verify_dates(e, NOW, 168).status == DATE_STATUS_CONFIRMED


def test_no_dates_and_weak_dates():
    assert verify_dates([], NOW, 168).flags == ["no_date_found"]
    v = verify_dates([ev("visible_text", "October 10, 2026")], NOW, 168)
    assert v.confidence == "low" and v.status == DATE_STATUS_UNCERTAIN


def test_future_date_flagged():
    v = verify_dates([ev("jsonld_datePublished", "2027-01-01T00:00:00Z")], NOW, 168)
    assert "future_publication_date" in v.flags and v.publication_date is None


def test_date_only_near_boundary_is_uncertain():
    # window start = 2026-10-04T12:00Z; a date-only "2026-10-04" straddles the boundary
    v = verify_dates([ev("jsonld_datePublished", "2026-10-04")], NOW, 168)
    assert v.status == DATE_STATUS_UNCERTAIN and "near_window_boundary" in v.flags


def test_timezone_offsets_agree_after_conversion():
    v = verify_dates([ev("jsonld_datePublished", "2026-10-10T15:30:00-04:00"),
                      ev("news_sitemap_publication_date", "2026-10-10T19:30:00Z", "published")], NOW, 168)
    assert v.confidence == "high" and not v.flags
