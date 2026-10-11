"""Local SQLite storage: scan history, per-scan work queue (for resume), articles,
seen-URL index (avoid reprocessing) and an HTTP response cache."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .http_client import FetchResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    trigger TEXT,
    window_hours REAL,
    reference_now TEXT,
    config_json TEXT,
    summary_json TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS queue (
    scan_id INTEGER NOT NULL,
    normalized_url TEXT NOT NULL,
    position INTEGER,
    item_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (scan_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    normalized_url TEXT NOT NULL,
    row_json TEXT NOT NULL,
    date_status TEXT,
    quality_score REAL,
    created_at TEXT NOT NULL,
    UNIQUE (scan_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS seen_urls (
    normalized_url TEXT PRIMARY KEY,
    canonical_url TEXT,
    first_seen TEXT NOT NULL,
    last_processed TEXT,
    last_scan_id INTEGER,
    last_status TEXT
);
CREATE TABLE IF NOT EXISTS article_texts (
    normalized_url TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    body_text TEXT
);
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    final_url TEXT,
    status INTEGER,
    headers_json TEXT,
    content BLOB
);
CREATE TABLE IF NOT EXISTS search_observations (
    normalized_url TEXT NOT NULL,
    scan_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    rank INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (normalized_url, scan_id, source, query)
);
CREATE INDEX IF NOT EXISTS idx_articles_scan ON articles(scan_id);
CREATE INDEX IF NOT EXISTS idx_queue_state ON queue(scan_id, state);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


class Storage:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if str(db_path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    # ---------------------------------------------------------------- scans
    def create_scan(self, config: dict, trigger: str, window_hours: float, reference_now: datetime) -> int:
        cur = self._exec(
            "INSERT INTO scans(started_at, status, trigger, window_hours, reference_now, config_json) "
            "VALUES (?, 'running', ?, ?, ?, ?)",
            (iso(utcnow()), trigger, window_hours, iso(reference_now), json.dumps(config, default=str)),
        )
        return int(cur.lastrowid)

    def update_scan(self, scan_id: int, **fields) -> None:
        if "summary" in fields:
            fields["summary_json"] = json.dumps(fields.pop("summary"), default=str)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE scans SET {cols} WHERE id = ?", (*fields.values(), scan_id))

    def finish_scan(self, scan_id: int, status: str, summary: dict, message: str = "") -> None:
        self.update_scan(scan_id, status=status, finished_at=iso(utcnow()), summary=summary, message=message)

    def get_scan(self, scan_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return self._scan_dict(row) if row else None

    def list_scans(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._scan_dict(r) for r in rows]

    def latest_resumable_scan(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM scans WHERE status IN ('running', 'interrupted', 'stopped') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._scan_dict(row) if row else None

    def mark_stale_running_as_interrupted(self) -> int:
        cur = self._exec("UPDATE scans SET status = 'interrupted' WHERE status = 'running'")
        return cur.rowcount

    @staticmethod
    def _scan_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        d["config"] = json.loads(d.pop("config_json") or "{}")
        return d

    # ---------------------------------------------------------------- queue (resume support)
    def enqueue(self, scan_id: int, entries: list[tuple[str, dict]]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO queue(scan_id, normalized_url, position, item_json) VALUES (?, ?, ?, ?)",
                [(scan_id, url, i, json.dumps(item, default=str)) for i, (url, item) in enumerate(entries)],
            )
            self.conn.commit()

    def queue_items(self, scan_id: int, state: str | None = None) -> list[tuple[str, dict, str]]:
        sql = "SELECT normalized_url, item_json, state FROM queue WHERE scan_id = ?"
        params: tuple = (scan_id,)
        if state:
            sql += " AND state = ?"
            params = (scan_id, state)
        rows = self.conn.execute(sql + " ORDER BY position", params).fetchall()
        return [(r["normalized_url"], json.loads(r["item_json"]), r["state"]) for r in rows]

    def mark_queue(self, scan_id: int, normalized_url: str, state: str) -> None:
        self._exec("UPDATE queue SET state = ? WHERE scan_id = ? AND normalized_url = ?",
                   (state, scan_id, normalized_url))

    # ---------------------------------------------------------------- articles
    def save_article(self, scan_id: int, normalized_url: str, row: dict, body_text: str = "") -> None:
        now = iso(utcnow())
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO articles(scan_id, normalized_url, row_json, date_status, quality_score, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, normalized_url, json.dumps(row, default=str), row.get("date_status"),
                 row.get("quality_score") if row.get("quality_score") not in ("", None) else None, now),
            )
            self.conn.execute(
                "INSERT INTO seen_urls(normalized_url, canonical_url, first_seen, last_processed, last_scan_id, last_status) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(normalized_url) DO UPDATE SET canonical_url=excluded.canonical_url, "
                "last_processed=excluded.last_processed, last_scan_id=excluded.last_scan_id, last_status=excluded.last_status",
                (normalized_url, row.get("canonical_url", ""), now, now, scan_id, row.get("extraction_status", "")),
            )
            if body_text:
                self.conn.execute(
                    "INSERT OR REPLACE INTO article_texts(normalized_url, processed_at, body_text) VALUES (?, ?, ?)",
                    (normalized_url, now, body_text[:200000]),
                )
            self.conn.commit()

    def update_article_row(self, scan_id: int, normalized_url: str, row: dict) -> None:
        self._exec(
            "UPDATE articles SET row_json = ?, quality_score = ?, date_status = ? WHERE scan_id = ? AND normalized_url = ?",
            (json.dumps(row, default=str), row.get("quality_score") if row.get("quality_score") not in ("", None) else None,
             row.get("date_status"), scan_id, normalized_url),
        )

    def articles_for_scan(self, scan_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT row_json FROM articles WHERE scan_id = ? ORDER BY id", (scan_id,)).fetchall()
        return [json.loads(r["row_json"]) for r in rows]

    def all_articles(self, limit: int = 5000) -> list[dict]:
        rows = self.conn.execute("SELECT row_json FROM articles ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["row_json"]) for r in rows]

    def is_seen(self, normalized_url: str) -> bool:
        row = self.conn.execute("SELECT last_status FROM seen_urls WHERE normalized_url = ?",
                                (normalized_url,)).fetchone()
        # Failed fetches may be retried in a later scan; successful ones are not reprocessed.
        return bool(row) and row["last_status"] not in ("fetch_failed", "")

    def seen_canonical(self, canonical_url: str, exclude: str = "") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_urls WHERE canonical_url = ? AND normalized_url != ? AND last_status NOT IN ('fetch_failed', '')",
            (canonical_url, exclude)).fetchone()
        return bool(row)

    def recent_texts(self, days: float, exclude_urls: set[str]) -> list[tuple[str, str]]:
        since = iso(utcnow() - timedelta(days=days))
        rows = self.conn.execute("SELECT normalized_url, body_text FROM article_texts WHERE processed_at >= ?",
                                 (since,)).fetchall()
        return [(r["normalized_url"], r["body_text"]) for r in rows if r["normalized_url"] not in exclude_urls]

    # ---------------------------------------------------------------- search positions
    def add_search_observations(self, normalized_url: str, scan_id: int, ranks: list[dict]) -> None:
        if not ranks:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO search_observations(normalized_url, scan_id, source, query, rank, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(normalized_url, scan_id, r["source"], r["query"], int(r["rank"]), r["observed_at"]) for r in ranks],
            )
            self.conn.commit()

    def search_position_history(self, normalized_url: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT scan_id, source, query, rank, observed_at FROM search_observations WHERE normalized_url = ? "
            "ORDER BY observed_at", (normalized_url,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- HTTP cache (implements http_client.Cache)
    def get(self, url: str, max_age_seconds: float) -> FetchResult | None:
        if max_age_seconds <= 0:
            return None
        row = self.conn.execute("SELECT * FROM http_cache WHERE url = ?", (url,)).fetchone()
        if not row or time.time() - row["fetched_at"] > max_age_seconds:
            return None
        return FetchResult(url=url, final_url=row["final_url"], status=row["status"], content=row["content"] or b"",
                           headers=json.loads(row["headers_json"] or "{}"))

    def put(self, result: FetchResult) -> None:
        if len(result.content) > 5_000_000:
            return
        self._exec(
            "INSERT OR REPLACE INTO http_cache(url, fetched_at, final_url, status, headers_json, content) VALUES (?, ?, ?, ?, ?, ?)",
            (result.url, time.time(), result.final_url, result.status,
             json.dumps({k: v for k, v in result.headers.items() if k in ("content-type", "last-modified", "location")}),
             result.content),
        )

    def prune_cache(self, max_age_seconds: float = 7 * 86400) -> None:
        self._exec("DELETE FROM http_cache WHERE fetched_at < ?", (time.time() - max_age_seconds,))
