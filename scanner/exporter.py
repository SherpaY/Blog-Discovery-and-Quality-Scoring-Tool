"""CSV exports with dated filenames."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

EXPORT_COLUMNS = [
    "scan_date", "title", "url", "canonical_url", "domain", "user_keyword", "discovery_query",
    "discovery_source", "publication_date", "modification_date", "date_confidence", "date_evidence",
    "author", "article_type", "word_count", "heading_count", "paragraph_count", "external_link_count",
    "quality_score", "quality_category", "originality_score", "depth_score", "evidence_score",
    "usefulness_score", "readability_score", "quality_flags", "explanation", "extraction_status", "http_status",
]
# Extra transparency columns appended after the required ones.
EXTRA_COLUMNS = [
    "date_status", "date_flags", "publication_date_local", "query_origin", "quality_signals", "strengths",
    "similar_to", "scoring_notes", "extraction_notes", "error", "llm_notes", "scan_id",
]
ALL_COLUMNS = EXPORT_COLUMNS + EXTRA_COLUMNS

EXPORT_KINDS = {
    "all_articles": "All discovered articles",
    "confirmed_recent": "Confirmed recent articles",
    "low_quality": "Potentially low-quality articles (confirmed recent, below threshold)",
    "needs_date_review": "Articles requiring date verification",
    "failed_extractions": "Failed extractions",
}

OK_EXTRACTION = ("ok", "thin_content")


def is_scored(row: dict) -> bool:
    return row.get("quality_score") not in (None, "")


def is_flagged(row: dict, threshold: float) -> bool:
    return is_scored(row) and float(row["quality_score"]) < threshold


def select_rows(rows: list[dict], kind: str, threshold: float) -> list[dict]:
    if kind == "all_articles":
        return rows
    if kind == "confirmed_recent":
        return [r for r in rows if r.get("date_status") == "confirmed_recent" and r.get("extraction_status") in OK_EXTRACTION]
    if kind == "low_quality":
        return [r for r in rows if r.get("date_status") == "confirmed_recent" and is_flagged(r, threshold)]
    if kind == "needs_date_review":
        return [r for r in rows if r.get("date_status") == "uncertain" and r.get("extraction_status") in OK_EXTRACTION]
    if kind == "failed_extractions":
        return [r for r in rows if r.get("extraction_status") not in OK_EXTRACTION
                and not str(r.get("extraction_status", "")).startswith("skipped")]
    raise ValueError(f"unknown export kind {kind}")


def write_csv(rows: list[dict], path: str | Path, columns: list[str] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = columns or ALL_COLUMNS
    # utf-8-sig so Excel/Numbers on macOS detect UTF-8 correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c, "")) for c in cols})
    return path


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    if isinstance(value, float):
        return f"{value:.1f}"
    return value


def export_scan(rows: list[dict], output_dir: str | Path, threshold: float, scan_id: int | None = None,
                when: datetime | None = None) -> dict[str, Path]:
    """Write all five exports into output/<YYYY-MM-DD>/ with timestamped names."""
    when = (when or datetime.now(timezone.utc)).astimezone()
    folder = Path(output_dir) / when.strftime("%Y-%m-%d")
    stamp = when.strftime("%Y%m%d_%H%M")
    suffix = f"_scan{scan_id}" if scan_id is not None else ""
    out = {}
    for kind in EXPORT_KINDS:
        out[kind] = write_csv(select_rows(rows, kind, threshold), folder / f"{kind}_{stamp}{suffix}.csv")
    return out
