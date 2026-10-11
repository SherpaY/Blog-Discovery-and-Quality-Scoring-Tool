"""End-to-end scan: discovery -> fetch -> extract -> verify dates -> score -> store -> export."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import resolve_path
from .date_verification import DateEvidence, make_evidence, to_local, verify_dates
from .discovery import item_from_dict, item_to_dict, run_discovery
from .exporter import OK_EXTRACTION, export_scan, is_flagged, select_rows
from .extractor import ArticleData, extract_article, is_html_response
from .http_client import Fetcher, StopRequested
from .llm_review import OllamaReviewer
from .models import DiscoveredItem
from .quality_scoring import QualityScorer
from .similarity import max_similarity
from .storage import Storage, iso
from .topics import Query, expand_keywords, match_keywords
from .urlutils import get_domain, normalize_url

log = logging.getLogger(__name__)

try:  # fcntl exists on macOS/Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class ScanAlreadyRunning(RuntimeError):
    pass


class ScanLock:
    """Prevents overlapping scans (e.g. a scheduled run while a manual scan is going).

    Uses an OS-level file lock, which is released automatically if the process dies,
    so a crash never leaves a stale lock behind.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self._fh.close()
                self._fh = None
                raise ScanAlreadyRunning("another scan is already running") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def is_locked(self) -> bool:
        if fcntl is None or not self.path.exists():
            return False
        with open(self.path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return False
            except OSError:
                return True


def stop_flag_path(config: dict) -> Path:
    return resolve_path(config, "data_dir") / "stop.flag"


def status_path(config: dict) -> Path:
    return resolve_path(config, "data_dir") / "scan_status.json"


def request_stop(config: dict) -> None:
    stop_flag_path(config).write_text(datetime.now(timezone.utc).isoformat())


def read_status(config: dict) -> dict:
    try:
        return json.loads(status_path(config).read_text())
    except (OSError, ValueError):
        return {}


def article_to_dict(a: ArticleData) -> dict:
    d = asdict(a)
    d.pop("date_evidence", None)
    d.pop("body_text", None)
    return d


def article_from_dict(d: dict, body_text: str) -> ArticleData:
    d = dict(d)
    d["headings"] = [tuple(h) for h in d.get("headings", [])]
    d["blocks"] = [tuple(b) for b in d.get("blocks", [])]
    a = ArticleData(**{k: v for k, v in d.items() if k in ArticleData.__dataclass_fields__})
    a.body_text = body_text
    return a


def search_position_fields(current: list[dict], history: list[dict], per_page: int = 10) -> dict:
    """Observed positions in relevance-ranked news-search feeds (Google/Bing News RSS).

    These are positions within the feed results at scan time - NOT Google web search
    rankings, which cannot be obtained free and legitimately (see README).
    """
    empty = {"search_position_best": "", "search_position_avg": "", "search_result_page": "",
             "search_position_observations": 0, "search_position_details": ""}
    if not current and not history:
        return empty
    best = min((r["rank"] for r in current), default=None)
    all_ranks = [r["rank"] for r in history] or [r["rank"] for r in current]
    avg = sum(all_ranks) / len(all_ranks)
    ref = best if best is not None else min(all_ranks)
    page = (ref - 1) // per_page + 1
    details = "; ".join(f'{r["source"]} "{r["query"]}" #{r["rank"]} at {r["observed_at"]}' for r in current)
    if len(history) > len(current):
        details += f" | {len(history)} observations across scans"
    return {
        "search_position_best": best if best is not None else "",
        "search_position_avg": round(avg, 1),
        "search_result_page": f"page {page} (positions {(page - 1) * per_page + 1}-{page * per_page}, if 10 per page)",
        "search_position_observations": len(all_ranks),
        "search_position_details": details,
    }


class ScanRunner:
    def __init__(self, config: dict, session=None, resolver=None, now: datetime | None = None,
                 progress: Callable[[dict], None] | None = None, db_path: str | Path | None = None):
        self.config = config
        self.data_dir = resolve_path(config, "data_dir")
        self.output_dir = resolve_path(config, "output_dir")
        self.fixed_now = now
        self.session = session
        self.resolver = resolver
        self.progress_cb = progress or (lambda s: None)
        self.db_path = Path(db_path) if db_path else self.data_dir / "scanner.db"
        self.status: dict = {}

    # ------------------------------------------------------------------ status helpers
    def _status(self, **fields) -> None:
        self.status.update(fields)
        self.status["updated"] = iso(datetime.now(timezone.utc))
        try:
            status_path(self.config).write_text(json.dumps(self.status, default=str))
        except OSError:
            pass
        self.progress_cb(dict(self.status))

    def _stop_requested(self) -> bool:
        return stop_flag_path(self.config).exists()

    # ------------------------------------------------------------------ main entry
    def run(self, trigger: str = "manual", resume: bool = False) -> dict:
        lock = ScanLock(self.data_dir / "scan.lock")
        lock.acquire()
        storage = Storage(self.db_path)
        try:
            return self._run_locked(storage, trigger, resume)
        finally:
            storage.close()
            lock.release()

    def _run_locked(self, storage: Storage, trigger: str, resume: bool) -> dict:
        stop_flag_path(self.config).unlink(missing_ok=True)
        cfg = self.config
        scan = storage.latest_resumable_scan() if resume else None
        if not resume:
            storage.mark_stale_running_as_interrupted()
        if scan:
            scan_id = scan["id"]
            cfg = {**(scan["config"] or cfg), "paths": self.config.get("paths", {})}
            self.config = cfg
            now = datetime.fromisoformat(scan["reference_now"].replace("Z", "+00:00"))
            storage.update_scan(scan_id, status="running", message="resumed")
            log.info("Resuming scan %s", scan_id)
            summary = scan.get("summary") or {}
        else:
            now = (self.fixed_now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            window = float(cfg["discovery"].get("window_hours", 168))
            scan_id = storage.create_scan(cfg, trigger, window, now)
            summary = {}
        self.status = {"scan_id": scan_id, "state": "running", "phase": "starting", "processed": 0, "total": 0,
                       "message": "", "started": iso(datetime.now(timezone.utc))}
        self._status()
        disc = cfg.get("discovery") or {}
        window_hours = float(disc.get("window_hours", 168))
        fetcher = Fetcher(cfg.get("http") or {}, session=self.session, resolver=self.resolver, cache=storage,
                          stop_check=self._stop_requested)
        queries = expand_keywords(cfg.get("keywords") or [], cfg.get("topics") or {})
        state = "completed"
        message = ""
        started = time.time()
        try:
            if not scan or not storage.queue_items(scan_id):
                self._status(phase="discovery", message="Discovering URLs")
                report = run_discovery(cfg, fetcher, now, storage=storage,
                                       progress=lambda m: self._status(message=m))
                storage.enqueue(scan_id, [(normalize_url(i.url), item_to_dict(i)) for i in report.candidates])
                summary["discovery"] = {
                    "total_urls_found": report.total_found,
                    "unique_urls": report.unique_found,
                    "queued_for_processing": len(report.candidates),
                    "rejected_before_fetch": report.rejection_counts(),
                    "methods": {k: {"name": m.name, "enabled": m.enabled, "ok": m.ok, "found": m.found,
                                    "messages": m.messages[-40:]} for k, m in report.methods.items()},
                    "search_sources": [asdict(s) for s in report.search_statuses],
                    "queries": [{"query": q.text, "origin": q.origin_label, "user_keyword": q.user_keyword}
                                for q in report.queries],
                    "suggested_phrases_from_titles": [q.text for q in report.suggested_phrases],
                }
                storage.update_scan(scan_id, summary=summary)

            pending = storage.queue_items(scan_id, "pending")
            total = len(storage.queue_items(scan_id))
            done_before = total - len(pending)
            self._status(phase="extraction", total=total, processed=done_before)
            seen_canonicals = {r.get("canonical_url") for r in storage.articles_for_scan(scan_id) if r.get("canonical_url")}
            for i, (key, item_dict, _) in enumerate(pending):
                if self._stop_requested():
                    raise StopRequested()
                item = item_from_dict(item_dict)
                self._status(processed=done_before + i, message=f"Fetching {item.url}")
                row, article = self._process_item(fetcher, item, now, window_hours, queries, seen_canonicals, storage)
                storage.add_search_observations(key, scan_id, item.search_ranks)
                row.update(search_position_fields(item.search_ranks, storage.search_position_history(key)))
                row["scan_id"] = scan_id
                row["scan_date"] = iso(now)
                if article is not None:
                    row["_article"] = article_to_dict(article)
                storage.save_article(scan_id, key, row, article.body_text if article else "")
                storage.mark_queue(scan_id, key, "done")
            self._status(processed=total, phase="scoring", message="Scoring quality")
            self._score_all(storage, scan_id, cfg)
        except StopRequested:
            state = "stopped"
            message = "Stopped by user; run with --resume (or press Resume) to continue"
            log.warning(message)
            # Score whatever was extracted so partial exports are still useful.
            try:
                self._score_all(storage, scan_id, cfg)
            except Exception:  # noqa: BLE001
                log.exception("scoring partial results failed")
        except Exception as exc:  # noqa: BLE001
            state = "failed"
            message = f"{type(exc).__name__}: {exc}"
            log.exception("scan %s failed", scan_id)
        finally:
            stop_flag_path(self.config).unlink(missing_ok=True)

        rows = [self._public_row(r) for r in storage.articles_for_scan(scan_id)]
        threshold = float((cfg.get("quality") or {}).get("flag_threshold", 60))
        exports = export_scan(rows, self.output_dir, threshold, scan_id=scan_id)
        summary["results"] = self._summarize(rows, threshold)
        summary["exports"] = {k: str(v) for k, v in exports.items()}
        summary["requests_made"] = fetcher.request_count
        summary["duration_seconds"] = round(time.time() - started, 1)
        summary["window"] = {"hours": window_hours, "from_utc": iso(now - timedelta(hours=window_hours)),
                             "to_utc": iso(now)}
        storage.finish_scan(scan_id, "completed" if state == "completed" else state, summary, message)
        self._status(state=state, phase="done", message=message or "Scan complete", summary=summary["results"])
        log.info("Scan %s %s: %s", scan_id, state, json.dumps(summary["results"]))
        storage.prune_cache()
        return {"scan_id": scan_id, "state": state, "message": message, **summary}

    # ------------------------------------------------------------------ per-URL processing
    def _process_item(self, fetcher: Fetcher, item: DiscoveredItem, now: datetime, window_hours: float,
                      queries: list[Query], seen_canonicals: set, storage: Storage) -> tuple[dict, ArticleData | None]:
        dcfg = self.config.get("dates") or {}
        row = {
            "title": item.title, "url": item.url, "canonical_url": "", "domain": item.domain,
            "user_keyword": item.user_keyword, "discovery_query": item.discovery_query,
            "query_origin": item.query_origin, "discovery_source": item.discovery_source,
            "extraction_status": "", "http_status": "", "error": "",
        }
        res = fetcher.fetch(item.url)
        row["http_status"] = res.status or ""
        evidence: list[DateEvidence] = list(item.date_evidence)
        article: ArticleData | None = None
        if not res.ok:
            err = res.error or f"http_{res.status}"
            row["error"] = err
            row["extraction_status"] = ("blocked_by_robots" if err.startswith("robots")
                                        else "unsafe_url_blocked" if err.startswith("unsafe")
                                        else "fetch_failed")
        elif not is_html_response(res.content_type, res.content):
            row["extraction_status"] = "not_html"
            row["error"] = f"content-type {res.content_type or 'unknown'}"
        else:
            article = extract_article(res.content, item.url, res.final_url)
            row["extraction_status"] = article.extraction_status
            row["extraction_notes"] = "; ".join(article.extraction_notes)
            row["title"] = article.title or item.title
            row["canonical_url"] = article.canonical_url or res.final_url or item.url
            row["domain"] = get_domain(res.final_url or item.url)
            row["author"] = article.author
            row["word_count"] = article.word_count
            row["heading_count"] = len(article.headings)
            row["paragraph_count"] = len(article.paragraphs)
            row["external_link_count"] = article.external_link_count
            evidence += article.date_evidence
            if res.headers.get("last-modified"):
                ev = make_evidence("http_last_modified", res.headers["last-modified"], kind="modified")
                if ev:
                    evidence.append(ev)
            canon = normalize_url(row["canonical_url"])
            if canon in seen_canonicals:
                row["extraction_status"] = "duplicate_canonical"
                row["error"] = "same canonical URL as another article in this scan"
                article = None
            else:
                seen_canonicals.add(canon)
                if canon != normalize_url(item.url) and not (self.config.get("discovery") or {}).get(
                        "reprocess_seen_urls") and storage.seen_canonical(canon, exclude=normalize_url(item.url)):
                    row["extraction_status"] = "skipped_already_processed"
                    row["error"] = "canonical URL processed in an earlier scan"
                    article = None
            # Keyword check against the article body for items not already matched by title/summary.
            if article is not None and (self.config.get("discovery") or {}).get("filter_by_keywords", True) and queries:
                matched = match_keywords(f"{article.title} {article.meta_description} {article.body_text[:4000]}",
                                         queries)
                if matched:
                    if not row["user_keyword"]:
                        row["user_keyword"] = " | ".join(dict.fromkeys(q.user_keyword for q in matched))
                        row["discovery_query"] = row["discovery_query"] or matched[0].text
                        row["query_origin"] = row["query_origin"] or matched[0].origin
                elif not item.user_keyword:
                    row["extraction_status"] = "skipped_no_keyword_match"
                    row["error"] = "article text does not mention any keyword"
                    article = None
                else:
                    row["extraction_notes"] = (row.get("extraction_notes", "") +
                                               "; found via keyword search but keyword not in extracted text").strip("; ")

        verdict = verify_dates(evidence, now, window_hours,
                               min_confidence=dcfg.get("min_confidence_for_confirmed", "medium"),
                               agreement_tolerance_hours=float(dcfg.get("agreement_tolerance_hours", 36)))
        row["publication_date"] = iso(verdict.publication_date)
        row["publication_date_local"] = (to_local(verdict.publication_date).strftime("%Y-%m-%d %H:%M %Z")
                                         if verdict.publication_date else "")
        row["modification_date"] = iso(verdict.modification_date)
        row["date_confidence"] = verdict.confidence
        row["date_status"] = verdict.status
        row["date_flags"] = ", ".join(verdict.flags)
        row["date_evidence"] = verdict.evidence_text
        log.info("date %s -> %s/%s via %s", item.url, verdict.status, verdict.confidence, verdict.primary_source)
        return row, article

    # ------------------------------------------------------------------ scoring
    def _score_all(self, storage: Storage, scan_id: int, cfg: dict) -> None:
        qcfg = cfg.get("quality") or {}
        scorer = QualityScorer(qcfg)
        reviewer = OllamaReviewer(cfg.get("ollama"))
        rows = storage.articles_for_scan(scan_id)
        scorable = [(normalize_url(r["url"]), r) for r in rows
                    if r.get("_article") and r.get("extraction_status") in OK_EXTRACTION]
        labels = [k for k, _ in scorable]
        texts = {}
        for k in labels:
            hit = storage.conn.execute("SELECT body_text FROM article_texts WHERE normalized_url = ?", (k,)).fetchone()
            texts[k] = hit[0] if hit else ""
        bodies = [texts.get(k, "") for k in labels]
        sim_cfg = qcfg.get("similarity") or {}
        refs = storage.recent_texts(float(sim_cfg.get("compare_with_recent_days", 7)), set(labels))
        sims = max_similarity(bodies, labels, [t for _, t in refs], [u for u, _ in refs],
                              method=sim_cfg.get("method", "tfidf"))
        keywords = cfg.get("keywords") or []
        threshold = float(qcfg.get("flag_threshold", 60))
        for (key, row), body, sim in zip(scorable, bodies, sims):
            article = article_from_dict(row["_article"], body)
            result = scorer.score(article, sim, keywords)
            row.update({
                "article_type": result.article_type,
                "quality_score": result.score if result.score is not None else "",
                "quality_category": result.category,
                "originality_score": result.criterion_scores.get("originality", ""),
                "depth_score": result.criterion_scores.get("depth", ""),
                "evidence_score": result.criterion_scores.get("evidence", ""),
                "usefulness_score": result.criterion_scores.get("usefulness", ""),
                "readability_score": result.criterion_scores.get("readability", ""),
                "quality_flags": ", ".join(result.flags),
                "quality_signals": result.signals_text,
                "strengths": "; ".join(result.strengths),
                "explanation": result.explanation,
                "scoring_notes": "; ".join(result.notes),
                "similar_to": f"{sim[1]} ({sim[0]:.2f})" if sim[1] and sim[0] >= 0.3 else "",
            })
            if reviewer.available and result.score is not None and result.score < threshold:
                row["llm_notes"] = reviewer.review(article.title, body)
            storage.update_article_row(scan_id, key, row)

    @staticmethod
    def _public_row(row: dict) -> dict:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    @staticmethod
    def _summarize(rows: list[dict], threshold: float) -> dict:
        processed = [r for r in rows if not str(r.get("extraction_status", "")).startswith(("skipped", "duplicate"))]
        return {
            "urls_processed": len(processed),
            "extracted_ok": sum(1 for r in processed if r.get("extraction_status") in OK_EXTRACTION),
            "confirmed_recent": len(select_rows(rows, "confirmed_recent", threshold)),
            "flagged_low_quality": len(select_rows(rows, "low_quality", threshold)),
            "needs_date_review": len(select_rows(rows, "needs_date_review", threshold)),
            "outside_window_rejected": sum(1 for r in processed if r.get("date_status") == "outside_window"),
            "failed_extractions": len(select_rows(rows, "failed_extractions", threshold)),
            "skipped_after_fetch": sum(1 for r in rows if str(r.get("extraction_status", "")).startswith(("skipped", "duplicate"))),
            "flagged_any_date": sum(1 for r in processed if is_flagged(r, threshold)),
        }


def run_scan(config: dict, trigger: str = "manual", resume: bool = False, **kwargs) -> dict:
    return ScanRunner(config, **kwargs).run(trigger=trigger, resume=resume)
