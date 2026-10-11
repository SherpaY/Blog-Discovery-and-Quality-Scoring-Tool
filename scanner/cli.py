"""Command-line interface: python -m scanner <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from . import scheduler
from .config import load_config, resolve_path
from .http_client import Fetcher
from .pipeline import ScanAlreadyRunning, ScanLock, read_status, request_stop, run_scan
from .rss_discovery import discover_feeds
from .search_discovery import probe_sources
from .sitemap_discovery import find_sitemaps
from .storage import Storage
from .topics import expand_keywords
from .urlutils import domain_to_base_url


def setup_logging(config: dict, verbose: bool = False) -> None:
    log_dir = resolve_path(config, "log_dir")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = RotatingFileHandler(log_dir / f"scanner_{datetime.now():%Y%m%d}.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    errh = RotatingFileHandler(log_dir / "errors.log", maxBytes=2_000_000, backupCount=2)
    errh.setLevel(logging.WARNING)
    errh.setFormatter(fmt)
    root.addHandler(errh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    for noisy in ("urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def print_summary(result: dict) -> None:
    r = result.get("results", {})
    d = result.get("discovery", {})
    print(f"\nScan {result.get('scan_id')} - {result.get('state')}" + (f" ({result['message']})" if result.get("message") else ""))
    if d:
        print(f"  URLs discovered:            {d.get('total_urls_found', 0)} ({d.get('unique_urls', 0)} unique)")
        print(f"  Queued for processing:      {d.get('queued_for_processing', 0)}")
        rej = d.get("rejected_before_fetch") or {}
        if rej:
            print("  Rejected before fetching:   " + ", ".join(f"{k}={v}" for k, v in sorted(rej.items())))
        for s in d.get("search_sources", []):
            print(f"  Search source {s['name']}: {'OK' if s['available'] else 'UNAVAILABLE'} - {s['message']}")
        for m in (d.get("methods") or {}).values():
            if m["enabled"]:
                print(f"  Method {m['name']}: {'ok' if m['ok'] else 'problems'}, {m['found']} URLs")
    print(f"  Processed:                  {r.get('urls_processed', 0)}")
    print(f"  Confirmed recent:           {r.get('confirmed_recent', 0)}")
    print(f"  Flagged low quality:        {r.get('flagged_low_quality', 0)}")
    print(f"  Need date review:           {r.get('needs_date_review', 0)}")
    print(f"  Outside window (rejected):  {r.get('outside_window_rejected', 0)}")
    print(f"  Failed extractions:         {r.get('failed_extractions', 0)}")
    print(f"  Skipped (dupes/no keyword): {r.get('skipped_after_fetch', 0)}")
    for kind, path in (result.get("exports") or {}).items():
        print(f"  CSV {kind}: {path}")


def cmd_scan(args, config) -> int:
    if args.keywords:
        config["keywords"] = args.keywords
    if args.domains is not None:
        config["competitor_domains"] = args.domains
    if args.window:
        config["discovery"]["window_hours"] = args.window
    if args.max_urls:
        config["discovery"]["max_urls"] = args.max_urls
    resume = args.resume
    if args.auto_resume:
        storage = Storage(resolve_path(config, "data_dir") / "scanner.db")
        last = storage.latest_resumable_scan()
        storage.close()
        if last and last.get("started_at", "") >= (datetime.now(timezone.utc) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"):
            resume = True
            logging.info("auto-resuming interrupted scan %s", last["id"])
    try:
        result = run_scan(config, trigger=args.trigger, resume=resume)
    except ScanAlreadyRunning:
        print("Another scan is already running - not starting a second one.")
        logging.warning("scan skipped: another scan is running")
        return 2
    print_summary(result)
    return 0 if result.get("state") in ("completed", "stopped") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner", description="Free recent-blog discovery and quality scoring")
    parser.add_argument("--config", help="path to a config YAML (default: config.yaml + data/user_config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="run a scan now")
    p.add_argument("--keywords", nargs="*", help="override keywords for this run")
    p.add_argument("--domains", nargs="*", help="override competitor domains for this run")
    p.add_argument("--window", type=int, choices=[24, 48, 72, 168], help="discovery window in hours")
    p.add_argument("--max-urls", type=int)
    p.add_argument("--resume", action="store_true", help="continue the last interrupted/stopped scan")
    p.add_argument("--auto-resume", action="store_true", help="resume if a scan from the last 20h was interrupted")
    p.add_argument("--trigger", default="manual")

    sub.add_parser("stop", help="ask a running scan to stop safely")
    sub.add_parser("status", help="show the running/last scan status")
    sub.add_parser("history", help="list previous scans")
    p = sub.add_parser("discover-feeds", help="find feeds and sitemaps for a domain")
    p.add_argument("domain")
    sub.add_parser("probe-search", help="check which free search sources work right now")
    p = sub.add_parser("expand", help="show generated query variations for keywords")
    p.add_argument("keywords", nargs="+")
    p = sub.add_parser("publish", help="write results as JSON for the shareable dashboard (site/)")
    p.add_argument("--keep", type=int, default=30, help="number of recent scans to publish")
    p.add_argument("--slim-db", action="store_true", help="drop the HTTP cache from the database afterwards")
    p = sub.add_parser("schedule", help="manage the daily launchd job (macOS)")
    p.add_argument("action", choices=["install", "uninstall", "status", "show"])
    p.add_argument("--hour", type=int)
    p.add_argument("--minute", type=int)

    args = parser.parse_args(argv)
    config = load_config(args.config) if args.config else load_config()
    setup_logging(config, args.verbose)

    if args.command == "scan":
        return cmd_scan(args, config)
    if args.command == "stop":
        if ScanLock(resolve_path(config, "data_dir") / "scan.lock").is_locked():
            request_stop(config)
            print("Stop requested; the scan will stop after the current request and save its progress.")
        else:
            print("No scan is running.")
        return 0
    if args.command == "status":
        status = read_status(config)
        print(json.dumps(status, indent=2) if status else "No scan has run yet.")
        return 0
    if args.command == "history":
        storage = Storage(resolve_path(config, "data_dir") / "scanner.db")
        for s in storage.list_scans(30):
            r = (s.get("summary") or {}).get("results", {})
            print(f"#{s['id']:<4} {s['started_at']}  {s['status']:<11} trigger={s['trigger']:<9} "
                  f"processed={r.get('urls_processed', '-')} confirmed={r.get('confirmed_recent', '-')} "
                  f"flagged={r.get('flagged_low_quality', '-')}")
        return 0
    if args.command == "discover-feeds":
        fetcher = Fetcher(config.get("http") or {})
        base = domain_to_base_url(args.domain)
        feeds, msgs = discover_feeds(fetcher, base)
        sitemaps, smsgs = find_sitemaps(fetcher, base)
        for m in msgs + smsgs:
            print("  " + m)
        print("Feeds:", *(feeds or ["(none found)"]), sep="\n  ")
        print("Sitemaps:", *sitemaps, sep="\n  ")
        return 0
    if args.command == "probe-search":
        fetcher = Fetcher(config.get("http") or {})
        for s in probe_sources(fetcher):
            print(f"{'OK  ' if s.available else 'FAIL'} {s.name}: {s.message}")
        return 0
    if args.command == "expand":
        for q in expand_keywords(args.keywords, config.get("topics") or {}):
            print(f"{q.text:<45} {q.origin_label}")
        return 0
    if args.command == "publish":
        from .dashboard import publish, slim_database

        db = resolve_path(config, "data_dir") / "scanner.db"
        index = publish(db, keep=args.keep)
        print(f"Published {len(index['scans'])} scan(s) to site/data/. Commit and push to update the dashboard.")
        if args.slim_db:
            slim_database(db)
        return 0
    if args.command == "schedule":
        sched = config.get("schedule") or {}
        hour = args.hour if args.hour is not None else int(sched.get("hour", 7))
        minute = args.minute if args.minute is not None else int(sched.get("minute", 30))
        if args.action == "install":
            print(scheduler.install(hour, minute))
        elif args.action == "uninstall":
            print(scheduler.uninstall())
        elif args.action == "status":
            print(scheduler.status())
        else:
            print(scheduler.plist_content(hour, minute))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
