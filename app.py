"""Local web interface (Streamlit). Run with:  streamlit run app.py

Scans run in a separate background process (python -m scanner scan) so the UI
stays responsive, a stop request is handled safely between requests, and the
same lock prevents overlap with scheduled runs.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from dateutil import tz

from scanner import scheduler
from scanner.config import PROJECT_ROOT, USER_CONFIG_PATH, load_config, resolve_path, save_user_config
from scanner.exporter import ALL_COLUMNS, EXPORT_KINDS, select_rows
from scanner.http_client import Fetcher
from scanner.pipeline import ScanLock, read_status, request_stop
from scanner.rss_discovery import discover_feeds
from scanner.search_discovery import ALL_SOURCES, probe_sources
from scanner.sitemap_discovery import find_sitemaps
from scanner.storage import Storage
from scanner.topics import expand_keywords
from scanner.urlutils import domain_to_base_url

st.set_page_config(page_title="Blog Quality Scanner", page_icon="🔎", layout="wide")
LOCAL_TZ = tz.tzlocal()
WINDOWS = {24: "Last 24 hours", 48: "Last 48 hours", 72: "Last 72 hours", 168: "Last 7 days"}
SOURCE_LABELS = {name: cls.label for name, cls in ALL_SOURCES.items()}


def lines(text: str) -> list[str]:
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def get_config() -> dict:
    return load_config()


def scan_running(cfg: dict) -> bool:
    return ScanLock(resolve_path(cfg, "data_dir") / "scan.lock").is_locked()


def start_scan(cfg: dict, resume: bool = False) -> None:
    args = [sys.executable, "-m", "scanner", "scan", "--trigger", "ui"]
    if resume:
        args.append("--resume")
    log = open(resolve_path(cfg, "log_dir") / "ui_scan_stdout.log", "a")
    subprocess.Popen(args, cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def storage(cfg: dict) -> Storage:
    return Storage(resolve_path(cfg, "data_dir") / "scanner.db")


# ============================================================================ sidebar: settings
cfg = get_config()
with st.sidebar:
    st.header("Settings")
    with st.form("settings"):
        kw_text = st.text_area("Keywords / topics (one per line)", "\n".join(cfg.get("keywords") or []), height=110)
        dom_text = st.text_area("Competitor domains (optional, one per line)",
                                "\n".join(cfg.get("competitor_domains") or []), height=90)
        disc = cfg["discovery"]
        window = st.selectbox("Discovery period", list(WINDOWS), format_func=WINDOWS.get,
                              index=list(WINDOWS).index(int(disc.get("window_hours", 168)))
                              if int(disc.get("window_hours", 168)) in WINDOWS else 3)
        max_urls = st.number_input("Maximum URLs to process", 5, 2000, int(disc.get("max_urls", 120)), step=5)
        threshold = st.slider("Flag articles scoring below", 0, 100, int(cfg["quality"].get("flag_threshold", 60)))
        filter_kw = st.checkbox("Only keep articles that mention my keywords", bool(disc.get("filter_by_keywords", True)))
        st.markdown("**Discovery methods**")
        m = disc.get("methods") or {}
        use_search = st.checkbox("Keyword search sources (free feeds)", bool(m.get("search", True)))
        use_rss = st.checkbox("My RSS/Atom feeds", bool(m.get("rss", True)))
        use_sitemap = st.checkbox("XML sitemaps", bool(m.get("sitemap", True)))
        use_seed = st.checkbox("Competitor domains", bool(m.get("seed_domains", True)))
        st.markdown("**Search sources**")
        src_cfg = disc.get("search_sources") or {}
        src_choice = {name: st.checkbox(label, bool(src_cfg.get(name)), key=f"src_{name}")
                      for name, label in SOURCE_LABELS.items()}
        with st.expander("Extra feeds and sitemaps"):
            feeds_text = st.text_area("Feed URLs", "\n".join(disc.get("feeds") or []), height=80)
            sitemaps_text = st.text_area("Sitemap URLs", "\n".join(disc.get("sitemaps") or []), height=80)
        reprocess = st.checkbox("Re-process URLs seen in earlier scans", bool(disc.get("reprocess_seen_urls", False)))
        if st.form_submit_button("Save settings", type="primary"):
            new = copy.deepcopy(cfg)
            new["keywords"] = lines(kw_text)
            new["competitor_domains"] = lines(dom_text)
            new["discovery"].update({
                "window_hours": int(window), "max_urls": int(max_urls), "filter_by_keywords": filter_kw,
                "reprocess_seen_urls": reprocess, "feeds": lines(feeds_text), "sitemaps": lines(sitemaps_text),
                "methods": {"search": use_search, "rss": use_rss, "sitemap": use_sitemap, "seed_domains": use_seed},
                "search_sources": src_choice,
            })
            new["quality"]["flag_threshold"] = int(threshold)
            save_user_config({k: new[k] for k in ("keywords", "competitor_domains", "discovery", "quality")})
            st.success(f"Saved to {USER_CONFIG_PATH.relative_to(PROJECT_ROOT)}")
            st.rerun()
    st.caption("Settings are stored locally in data/user_config.yaml.")

# ============================================================================ main
st.title("Recent Blog Discovery & Quality Scanner")
st.caption("Free and local. Discovery covers only what free sources expose - it is never a complete view of the web. "
           "Quality scores are screening signals for manual review, not verdicts.")

tab_scan, tab_results, tab_history, tab_tools = st.tabs(["Scan", "Results", "History", "Tools & schedule"])

# ---------------------------------------------------------------------------- scan tab
with tab_scan:
    running = scan_running(cfg)
    c1, c2, c3 = st.columns(3)
    if c1.button("Start scan", type="primary", disabled=running or not cfg.get("keywords")):
        start_scan(cfg)
        st.toast("Scan started")
    if c2.button("Stop scan", disabled=not running):
        request_stop(cfg)
        st.toast("Stop requested - progress is saved and can be resumed")
    db = storage(cfg)
    resumable = db.latest_resumable_scan()
    if c3.button("Resume interrupted scan", disabled=running or not resumable,
                 help="Continue the last stopped or interrupted scan where it left off"):
        start_scan(cfg, resume=True)
        st.toast("Resuming scan")
    db.close()

    @st.fragment(run_every="2s")
    def live_status():
        status = read_status(cfg)
        is_running = scan_running(cfg)
        if not status:
            st.info("No scan has run yet. Add keywords in the sidebar, save, then press **Start scan**.")
            return
        state = status.get("state", "")
        if is_running:
            total = max(1, int(status.get("total") or 0))
            done = int(status.get("processed") or 0)
            st.progress(min(1.0, done / total) if status.get("phase") == "extraction" else 0.02,
                        text=f"Scan #{status.get('scan_id')} - {status.get('phase')}: {done}/{status.get('total') or '?'}")
            st.caption(status.get("message", ""))
        else:
            if state == "running":
                st.warning("The last scan stopped unexpectedly (e.g. the Mac slept or the app closed). "
                           "Press **Resume interrupted scan** to continue.")
            else:
                st.success(f"Scan #{status.get('scan_id')} {state}. {status.get('message', '')}")
            res = status.get("summary") or {}
            if res:
                cols = st.columns(6)
                for col, (label, key) in zip(cols, [("Processed", "urls_processed"), ("Confirmed recent", "confirmed_recent"),
                                                    ("Flagged low quality", "flagged_low_quality"),
                                                    ("Need date review", "needs_date_review"),
                                                    ("Outside window", "outside_window_rejected"),
                                                    ("Failed", "failed_extractions")]):
                    col.metric(label, res.get(key, 0))

    live_status()

    db = storage(cfg)
    scans = db.list_scans(1)
    db.close()
    if scans and scans[0].get("summary", {}).get("discovery"):
        d = scans[0]["summary"]["discovery"]
        with st.expander(f"Discovery report for scan #{scans[0]['id']}", expanded=False):
            st.write(f"URLs found: **{d.get('total_urls_found', 0)}** ({d.get('unique_urls', 0)} unique), "
                     f"queued: **{d.get('queued_for_processing', 0)}**")
            if d.get("search_sources"):
                st.markdown("**Search sources**")
                for s_ in d["search_sources"]:
                    st.write(("✅ " if s_["available"] else "⛔ ") + f"**{s_['name']}** - {s_['message']}")
            if d.get("rejected_before_fetch"):
                st.markdown("**Rejected before fetching**")
                st.write(d["rejected_before_fetch"])
            q = pd.DataFrame(d.get("queries") or [])
            if not q.empty:
                st.markdown("**Queries used** (user keywords vs automatically generated variations - "
                            "no search volumes or difficulty are estimated)")
                st.dataframe(q, hide_index=True, width="stretch")
            if d.get("suggested_phrases_from_titles"):
                st.markdown("**Phrases seen repeatedly in discovered titles** (suggestions only): " +
                            ", ".join(d["suggested_phrases_from_titles"]))
            for key, m in (d.get("methods") or {}).items():
                if m.get("enabled"):
                    st.markdown(f"**{m['name']}** - {'ok' if m['ok'] else 'problems'}, {m['found']} URLs")
                    st.code("\n".join(m.get("messages", [])[-25:]) or "(no messages)")

# ---------------------------------------------------------------------------- results tab
with tab_results:
    db = storage(cfg)
    scans = db.list_scans(100)
    if not scans:
        st.info("No results yet.")
    else:
        labels = {s["id"]: f"#{s['id']} - {s['started_at']} ({s['status']}, {s.get('window_hours') or '?'}h window)"
                  for s in scans}
        scan_id = st.selectbox("Scan", list(labels), format_func=labels.get)
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in db.articles_for_scan(scan_id)]
        threshold = float(cfg["quality"].get("flag_threshold", 60))
        view = st.radio("Show", list(EXPORT_KINDS), format_func=EXPORT_KINDS.get, horizontal=True, index=1)
        rows = select_rows(rows, view, threshold)
        df = pd.DataFrame(rows, columns=ALL_COLUMNS) if rows else pd.DataFrame(columns=ALL_COLUMNS)
        df["published_local"] = pd.to_datetime(df["publication_date"], utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ)
        for col in ("quality_score", "word_count", "search_position_best", "search_position_avg"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        f1, f2, f3, f4 = st.columns(4)
        domains = sorted(d for d in df["domain"].dropna().unique() if d)
        pick_domains = f1.multiselect("Domain", domains)
        keywords = sorted({k.strip() for v in df["user_keyword"].dropna() for k in str(v).split("|") if k.strip()})
        pick_kw = f2.multiselect("Keyword", keywords)
        wc_max = int(df["word_count"].max()) if df["word_count"].notna().any() else 5000
        wc = f3.slider("Word count", 0, max(wc_max, 100), (0, max(wc_max, 100)))
        qs = f4.slider("Quality score", 0, 100, (0, 100))
        dates = df["published_local"].dropna()
        if not dates.empty:
            d0, d1 = dates.min().date(), dates.max().date()
            dr = st.date_input("Publication date (your local time)", (d0, d1), min_value=d0 - timedelta(days=1),
                               max_value=d1 + timedelta(days=1))
        else:
            dr = None
        include_unscored = st.checkbox("Include unscored / undated rows", True)

        mask = pd.Series(True, index=df.index)
        if pick_domains:
            mask &= df["domain"].isin(pick_domains)
        if pick_kw:
            mask &= df["user_keyword"].fillna("").apply(lambda v: any(k in v for k in pick_kw))
        wc_ok = df["word_count"].between(*wc)
        q_ok = df["quality_score"].between(*qs)
        if include_unscored:
            wc_ok |= df["word_count"].isna()
            q_ok |= df["quality_score"].isna()
        mask &= wc_ok & q_ok
        if dr and isinstance(dr, tuple) and len(dr) == 2:
            d_ok = df["published_local"].dt.date.between(dr[0], dr[1])
            if include_unscored:
                d_ok |= df["published_local"].isna()
            mask &= d_ok.fillna(False)
        view_df = df[mask]

        st.write(f"**{len(view_df)}** article(s)")
        show_cols = ["quality_score", "quality_category", "title", "url", "domain", "published_local", "date_confidence",
                     "word_count", "article_type", "user_keyword", "search_position_best", "search_position_avg",
                     "explanation", "discovery_source", "extraction_status"]
        st.dataframe(
            view_df[show_cols], hide_index=True, width="stretch",
            column_config={
                "url": st.column_config.LinkColumn("URL", help="Click to open in your browser"),
                "quality_category": "Category", "title": "Title", "domain": "Domain",
                "date_confidence": "Date confidence", "word_count": "Words", "article_type": "Type",
                "user_keyword": "Keyword", "explanation": "Why flagged", "discovery_source": "Found via",
                "extraction_status": "Extraction",
                "quality_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
                "published_local": st.column_config.DatetimeColumn("Published (local)", format="YYYY-MM-DD HH:mm"),
                "search_position_best": st.column_config.NumberColumn(
                    "News-search pos.", help="Position in Google/Bing News RSS results at scan time "
                                             "(NOT Google web ranking)"),
                "search_position_avg": st.column_config.NumberColumn("Avg pos. (all scans)", format="%.1f"),
            },
        )

        d1c, d2c = st.columns(2)
        d1c.download_button("Download shown rows (CSV)", view_df[ALL_COLUMNS].to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"{view}_scan{scan_id}_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")
        all_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in db.articles_for_scan(scan_id)]
        flagged = pd.DataFrame(select_rows(all_rows, "low_quality", threshold), columns=ALL_COLUMNS)
        d2c.download_button(f"Download flagged only ({len(flagged)})", flagged.to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"low_quality_scan{scan_id}_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")

        if not view_df.empty:
            st.subheader("Article details")
            pick = st.selectbox("Article", view_df.index, format_func=lambda i: (view_df.at[i, "title"] or view_df.at[i, "url"])[:120])
            r = view_df.loc[pick]
            st.markdown(f"[{r['url']}]({r['url']})")
            a, b = st.columns(2)
            with a:
                st.markdown(f"**Score:** {r['quality_score'] if pd.notna(r['quality_score']) else 'not scored'} - "
                            f"{r['quality_category']}  \n**Type:** {r['article_type']}  \n**Explanation:** {r['explanation']}")
                st.markdown("**Criterion scores:** " + ", ".join(
                    f"{c} {r[c + '_score']}" for c in ("originality", "depth", "evidence", "usefulness", "readability")))
                if r.get("quality_signals"):
                    st.markdown("**Signals (points lost)**")
                    for sig in str(r["quality_signals"]).split("; "):
                        st.write("- " + sig)
                if r.get("strengths"):
                    st.markdown(f"**Strengths:** {r['strengths']}")
                if r.get("llm_notes"):
                    st.info(r["llm_notes"])
            with b:
                st.markdown(f"**Date status:** {r['date_status']} (confidence: {r['date_confidence']})  \n"
                            f"**Published:** {r['published_local'] if pd.notna(r['published_local']) else '-'}  \n"
                            f"**Modified (UTC):** {r['modification_date'] or '-'}  \n**Flags:** {r['date_flags'] or '-'}")
                st.markdown("**Date evidence**")
                st.code(str(r["date_evidence"]).replace(" | ", "\n") or "-")
                if r.get("search_position_details"):
                    st.markdown("**News-search positions** (Google/Bing News RSS at scan time - not Google web rankings)")
                    st.code(str(r["search_position_details"]).replace("; ", "\n"))
                st.markdown(f"**Discovered via:** {r['discovery_source']}  \n**Query:** {r['discovery_query']} "
                            f"({r['query_origin'] or '-'})  \n**Extraction:** {r['extraction_status']} "
                            f"(HTTP {r['http_status']}) {r['error'] or ''}")
    db.close()

# ---------------------------------------------------------------------------- history tab
with tab_history:
    db = storage(cfg)
    hist = []
    for s_ in db.list_scans(200):
        res = (s_.get("summary") or {}).get("results", {})
        hist.append({"scan": s_["id"], "started (UTC)": s_["started_at"], "finished (UTC)": s_.get("finished_at"),
                     "status": s_["status"], "trigger": s_["trigger"], "window h": s_.get("window_hours"),
                     "processed": res.get("urls_processed"), "confirmed": res.get("confirmed_recent"),
                     "flagged": res.get("flagged_low_quality"), "date review": res.get("needs_date_review"),
                     "failed": res.get("failed_extractions"), "note": s_.get("message")})
    db.close()
    st.dataframe(pd.DataFrame(hist), hide_index=True, width="stretch")
    st.caption(f"CSV exports are saved under {resolve_path(cfg, 'output_dir')} in dated folders. "
               f"Logs: {resolve_path(cfg, 'log_dir')}")

# ---------------------------------------------------------------------------- tools tab
with tab_tools:
    st.subheader("Feed & sitemap finder")
    dom = st.text_input("Domain or blog URL", placeholder="example.com/blog")
    if st.button("Find feeds and sitemaps") and dom:
        with st.spinner("Checking (respects robots.txt and rate limits)..."):
            fetcher = Fetcher(cfg.get("http") or {})
            base = domain_to_base_url(dom)
            feeds, msgs = discover_feeds(fetcher, base)
            sms, smsgs = find_sitemaps(fetcher, base)
        st.write("**Feeds:**", feeds or "none found")
        st.write("**Sitemaps:**", sms)
        st.code("\n".join(msgs + smsgs))

    st.subheader("Check search sources")
    st.caption("Tests each free keyword source right now. Blocked sources are reported, never worked around.")
    if st.button("Test search sources"):
        with st.spinner("Testing..."):
            for s_ in probe_sources(Fetcher(cfg.get("http") or {})):
                st.write(("✅ " if s_.available else "⛔ ") + f"**{s_.name}**: {s_.message}")

    st.subheader("Keyword variations preview")
    for q in expand_keywords(cfg.get("keywords") or [], cfg.get("topics") or {}):
        st.write(f"- `{q.text}` - {q.origin_label}")

    st.subheader("Daily schedule (macOS)")
    sched = cfg.get("schedule") or {}
    t = st.time_input("Run every day at", datetime.now().replace(hour=int(sched.get("hour", 7)),
                                                                 minute=int(sched.get("minute", 30))).time())
    st.caption("Runs only while your Mac is on and you are logged in. If it is asleep at that time, "
               "the scan runs when it wakes. Overlapping scans are prevented automatically.")
    sc1, sc2 = st.columns(2)
    if sc1.button("Install / update daily scan"):
        st.write(scheduler.install(t.hour, t.minute))
    if sc2.button("Remove daily scan"):
        st.write(scheduler.uninstall())
    st.caption("Status: " + scheduler.status())
