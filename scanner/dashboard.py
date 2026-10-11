"""Publish scan results as static JSON for the shareable web dashboard (site/).

The dashboard is plain HTML/JS, so it can be hosted for free on Vercel (or any
static host) and shared with teammates. Scans themselves run elsewhere (your Mac
or GitHub Actions) because they take minutes and need persistent storage.

Layout written:
  site/data/index.json             list of published scans (newest first)
  site/data/scans/<scan>.json      rows + discovery report for one scan
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT
from .exporter import ALL_COLUMNS, EXPORT_KINDS
from .quality_scoring import DEFAULT_WEIGHTS
from .storage import Storage

SITE_DATA = PROJECT_ROOT / "site" / "data"
CRITERIA = ("originality", "depth", "evidence", "usefulness", "readability")


def _scan_file_id(scan: dict) -> str:
    stamp = re.sub(r"[^0-9]", "", scan.get("started_at", ""))[:12] or "000000000000"
    return f"scan_{stamp}_{scan['id']}"


def _public_config(cfg: dict) -> dict:
    disc = cfg.get("discovery") or {}
    return {
        "keywords": cfg.get("keywords") or [],
        "competitor_domains": cfg.get("competitor_domains") or [],
        "window_hours": disc.get("window_hours"),
        "max_urls": disc.get("max_urls"),
        "flag_threshold": (cfg.get("quality") or {}).get("flag_threshold", 60),
        "weights": {**DEFAULT_WEIGHTS, **((cfg.get("quality") or {}).get("weights") or {})},
        "filter_by_keywords": disc.get("filter_by_keywords"),
        "search_sources": [k for k, v in (disc.get("search_sources") or {}).items() if v],
    }


def publish(db_path: str | Path, out_dir: str | Path | None = None, keep: int = 30) -> dict:
    """Write the newest `keep` finished scans from the database as dashboard JSON.

    Scans already published (e.g. from an earlier run whose database was lost)
    are kept in the index, so history survives a cache reset.
    """
    out = Path(out_dir) if out_dir else SITE_DATA
    scans_dir = out / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(db_path)
    try:
        written = []
        for scan in storage.list_scans(keep):
            if scan["status"] == "running":
                continue
            rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in storage.articles_for_scan(scan["id"])]
            summary = scan.get("summary") or {}
            entry = {
                "file": f"scans/{_scan_file_id(scan)}.json",
                "scan_id": scan["id"],
                "started_at": scan["started_at"],
                "finished_at": scan.get("finished_at"),
                "status": scan["status"],
                "trigger": scan.get("trigger"),
                "window_hours": scan.get("window_hours"),
                "results": summary.get("results", {}),
                "settings": _public_config(scan.get("config") or {}),
            }
            disc = summary.get("discovery", {})
            payload = {
                **entry,
                "message": scan.get("message"),
                "discovery": {
                    "total_urls_found": disc.get("total_urls_found"),
                    "unique_urls": disc.get("unique_urls"),
                    "queued_for_processing": disc.get("queued_for_processing"),
                    "rejected_before_fetch": disc.get("rejected_before_fetch", {}),
                    "search_sources": disc.get("search_sources", []),
                    "methods": {k: {"name": m.get("name"), "enabled": m.get("enabled"), "ok": m.get("ok"),
                                    "found": m.get("found"), "messages": (m.get("messages") or [])[-15:]}
                                for k, m in (disc.get("methods") or {}).items()},
                    "queries": disc.get("queries", []),
                    "suggested_phrases_from_titles": disc.get("suggested_phrases_from_titles", []),
                },
                "rows": [{c: r.get(c, "") for c in ALL_COLUMNS} for r in rows],
            }
            (out / entry["file"]).write_text(json.dumps(payload, default=str, ensure_ascii=False))
            written.append(entry)
    finally:
        storage.close()

    # Merge with scans published earlier, newest first, then prune to `keep`.
    index_path = out / "index.json"
    previous = []
    if index_path.exists():
        try:
            previous = json.loads(index_path.read_text()).get("scans", [])
        except ValueError:
            previous = []
    merged = {e["file"]: e for e in previous}
    merged.update({e["file"]: e for e in written})
    scans = sorted(merged.values(), key=lambda e: e.get("started_at") or "", reverse=True)
    kept, dropped = scans[:keep], scans[keep:]
    for e in dropped:
        (out / e["file"]).unlink(missing_ok=True)
    kept = [e for e in kept if (out / e["file"]).exists()]
    index = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "columns": ALL_COLUMNS,
        "views": EXPORT_KINDS,
        "criteria": CRITERIA,
        "scans": kept,
    }
    index_path.write_text(json.dumps(index, default=str, ensure_ascii=False, indent=1))
    return index


def slim_database(db_path: str | Path) -> None:
    """Drop the HTTP response cache before the database is stored between CI runs."""
    storage = Storage(db_path)
    try:
        storage.conn.execute("DELETE FROM http_cache")
        storage.conn.commit()
        storage.conn.execute("VACUUM")
    finally:
        storage.close()
