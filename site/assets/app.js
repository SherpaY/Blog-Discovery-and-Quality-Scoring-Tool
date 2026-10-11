// Static dashboard for Blog Quality Scanner results.
// All data comes from external websites, so it is only ever inserted as text
// (never innerHTML) and only http(s) links are rendered.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { index: null, scan: null, view: "confirmed_recent", sort: { key: "quality_score", asc: true } };
  const OK_EXTRACTION = ["ok", "thin_content"];

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "style") node.style.cssText = v; // CSSOM: allowed by the strict CSP
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function safeUrl(u) {
    try {
      const url = new URL(String(u));
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (e) { return null; }
  }

  function link(u, label) {
    const href = safeUrl(u);
    return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer nofollow", text: label || href }) : document.createTextNode(label || u || "");
  }

  const num = (v) => (v === "" || v === null || v === undefined || isNaN(Number(v)) ? null : Number(v));
  const isScored = (r) => num(r.quality_score) !== null;

  function fmtLocal(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function threshold() {
    return Number((state.scan && state.scan.settings && state.scan.settings.flag_threshold) || 60);
  }

  // Same selection rules as scanner/exporter.py
  function selectRows(rows, kind) {
    const t = threshold();
    switch (kind) {
      case "all_articles": return rows;
      case "confirmed_recent": return rows.filter((r) => r.date_status === "confirmed_recent" && OK_EXTRACTION.includes(r.extraction_status));
      case "low_quality": return rows.filter((r) => r.date_status === "confirmed_recent" && isScored(r) && num(r.quality_score) < t);
      case "needs_date_review": return rows.filter((r) => r.date_status === "uncertain" && OK_EXTRACTION.includes(r.extraction_status));
      case "failed_extractions": return rows.filter((r) => !OK_EXTRACTION.includes(r.extraction_status) && !String(r.extraction_status || "").startsWith("skipped"));
      default: return rows;
    }
  }

  function catClass(cat) {
    const c = String(cat || "").toLowerCase();
    if (c.startsWith("strong")) return "c-strong";
    if (c.startsWith("reasonable")) return "c-reasonable";
    if (c.startsWith("needs")) return "c-review";
    if (c.startsWith("potentially")) return "c-low";
    return "c-none";
  }

  function scoreColor(s) {
    if (s >= 80) return "var(--good)";
    if (s >= 60) return "var(--ok)";
    if (s >= 40) return "var(--warn)";
    return "var(--bad)";
  }

  function scoreCell(r) {
    const s = num(r.quality_score);
    if (s === null) return el("span", { class: "muted", text: "n/a" });
    const bar = el("span", { class: "bar" }, el("i", { style: `width:${Math.max(2, s)}%;background:${scoreColor(s)}` }));
    return el("span", { class: "score" }, bar, el("b", { text: s.toFixed(0) }));
  }

  // ------------------------------------------------------------------ loading
  async function getJSON(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }

  async function init() {
    try {
      state.index = await getJSON("data/index.json");
    } catch (e) {
      state.index = { scans: [] };
    }
    const scans = state.index.scans || [];
    if (!scans.length) { $("empty").hidden = false; return; }
    const sel = $("scanSelect");
    for (const s of scans) {
      const label = `${fmtLocal(s.started_at)} · ${s.window_hours ? Math.round(s.window_hours) + "h" : "?"} window · ${s.status}` +
        (s.results && s.results.confirmed_recent !== undefined ? ` · ${s.results.confirmed_recent} recent` : "");
      sel.append(el("option", { value: s.file, text: label }));
    }
    sel.addEventListener("change", () => loadScan(sel.value));
    const params = new URLSearchParams(location.hash.slice(1));
    if (params.get("view")) state.view = params.get("view");
    bindFilters();
    await loadScan(scans[0].file);
  }

  async function loadScan(file) {
    try {
      state.scan = await getJSON("data/" + file);
    } catch (e) {
      $("empty").hidden = false;
      $("empty").querySelector("h2").textContent = "Could not load scan data: " + e.message;
      return;
    }
    $("content").hidden = false;
    renderSettings();
    renderMetrics();
    renderReport();
    fillFilterOptions();
    renderViews();
    renderTable();
    const gen = state.index.generated_at ? ` Data published ${fmtLocal(state.index.generated_at)}.` : "";
    $("footnote").textContent = `Scan #${state.scan.scan_id} (${state.scan.trigger || "manual"}), started ${fmtLocal(state.scan.started_at)}.` +
      gen + " Times are shown in your browser's local timezone; CSV downloads use UTC.";
  }

  // ------------------------------------------------------------------ header sections
  function renderSettings() {
    const s = state.scan.settings || {};
    const box = $("settings");
    box.replaceChildren(
      el("span", {}, el("b", { text: "Keywords: " }), ...(s.keywords || []).map((k) => el("span", { class: "chip", text: k }))),
      (s.competitor_domains || []).length ? el("span", {}, el("b", { text: "Competitors: " }), ...s.competitor_domains.map((k) => el("span", { class: "chip", text: k }))) : null,
      el("span", {}, el("b", { text: "Window: " }), s.window_hours ? `${Math.round(s.window_hours)} h` : "?"),
      el("span", {}, el("b", { text: "Flag below: " }), String(threshold())),
    );
  }

  function renderMetrics() {
    const r = state.scan.results || {};
    const items = [
      ["Processed", r.urls_processed], ["Confirmed recent", r.confirmed_recent], ["Flagged low quality", r.flagged_low_quality],
      ["Need date review", r.needs_date_review], ["Outside window", r.outside_window_rejected], ["Failed", r.failed_extractions],
    ];
    $("metrics").replaceChildren(...items.map(([l, v]) =>
      el("div", { class: "card metric" }, el("div", { class: "v", text: v === undefined ? "–" : String(v) }), el("div", { class: "l", text: l }))));
  }

  function renderReport() {
    const d = state.scan.discovery || {};
    const body = $("reportBody");
    const parts = [
      el("p", { text: `URLs found: ${d.total_urls_found ?? "?"} (${d.unique_urls ?? "?"} unique) · queued for processing: ${d.queued_for_processing ?? "?"}` }),
    ];
    if ((d.search_sources || []).length) {
      parts.push(el("h3", { text: "Search sources" }));
      parts.push(el("ul", {}, d.search_sources.map((s) => el("li", {},
        el("span", { class: s.available ? "status-ok" : "status-bad", text: s.available ? "OK " : "Unavailable " }),
        el("b", { text: s.name }), " – ", s.message))));
    }
    const rej = d.rejected_before_fetch || {};
    if (Object.keys(rej).length) {
      parts.push(el("h3", { text: "Rejected before fetching" }));
      parts.push(el("ul", {}, Object.entries(rej).map(([k, v]) => el("li", { text: `${k.replace(/_/g, " ")}: ${v}` }))));
    }
    if ((d.queries || []).length) {
      parts.push(el("h3", { text: "Queries (user keywords vs generated variations – no search volumes are estimated)" }));
      parts.push(el("ul", {}, d.queries.map((q) => el("li", {}, el("code", { text: q.query }), " – ", q.origin))));
    }
    if ((d.suggested_phrases_from_titles || []).length) {
      parts.push(el("h3", { text: "Phrases seen repeatedly in discovered titles (suggestions only)" }));
      parts.push(el("p", { text: d.suggested_phrases_from_titles.join(", ") }));
    }
    for (const m of Object.values(d.methods || {})) {
      if (!m.enabled) continue;
      parts.push(el("h3", {}, `${m.name} – `, el("span", { class: m.ok ? "status-ok" : "status-bad", text: m.ok ? "ok" : "problems" }), `, ${m.found} URLs`));
      if ((m.messages || []).length) parts.push(el("pre", { class: "log", text: m.messages.join("\n") }));
    }
    if (state.scan.message) parts.push(el("p", { class: "muted", text: state.scan.message }));
    body.replaceChildren(...parts);
  }

  function renderViews() {
    const views = state.index.views || {};
    const rows = state.scan.rows || [];
    const nav = $("views");
    nav.replaceChildren(...Object.entries(views).map(([key, label]) => el("button", {
      type: "button", role: "tab", "aria-selected": String(key === state.view),
      onclick: () => { state.view = key; location.hash = "view=" + key; renderViews(); renderTable(); },
    }, label, el("span", { class: "n", text: `(${selectRows(rows, key).length})` }))));
  }

  // ------------------------------------------------------------------ filters
  function bindFilters() {
    for (const id of ["fText", "fDomain", "fKeyword", "fScoreMin", "fScoreMax", "fWordsMin", "fWordsMax", "fFrom", "fTo", "fUnscored"]) {
      $(id).addEventListener("input", renderTable);
    }
    $("reset").addEventListener("click", () => {
      for (const id of ["fText", "fDomain", "fKeyword", "fScoreMin", "fScoreMax", "fWordsMin", "fWordsMax", "fFrom", "fTo"]) $(id).value = "";
      $("fUnscored").checked = true;
      renderTable();
    });
    document.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sort = state.sort.key === key ? { key, asc: !state.sort.asc } : { key, asc: key !== "publication_date" };
      renderTable();
    }));
    $("dlShown").addEventListener("click", () => downloadCSV(filteredRows(), `${state.view}`));
    $("dlFlagged").addEventListener("click", () => downloadCSV(selectRows(state.scan.rows || [], "low_quality"), "low_quality"));
  }

  function fillFilterOptions() {
    const rows = state.scan.rows || [];
    const domains = [...new Set(rows.map((r) => r.domain).filter(Boolean))].sort();
    const keywords = [...new Set(rows.flatMap((r) => String(r.user_keyword || "").split("|").map((k) => k.trim())).filter(Boolean))].sort();
    const fill = (sel, values, allLabel) => {
      const cur = sel.value;
      sel.replaceChildren(el("option", { value: "", text: allLabel }), ...values.map((v) => el("option", { value: v, text: v })));
      sel.value = values.includes(cur) ? cur : "";
    };
    fill($("fDomain"), domains, "All domains");
    fill($("fKeyword"), keywords, "All keywords");
  }

  function filteredRows() {
    const rows = selectRows(state.scan.rows || [], state.view);
    const text = $("fText").value.trim().toLowerCase();
    const dom = $("fDomain").value, kw = $("fKeyword").value;
    const sMin = num($("fScoreMin").value), sMax = num($("fScoreMax").value);
    const wMin = num($("fWordsMin").value), wMax = num($("fWordsMax").value);
    const from = $("fFrom").value ? new Date($("fFrom").value + "T00:00:00") : null;
    const to = $("fTo").value ? new Date($("fTo").value + "T23:59:59") : null;
    const unscored = $("fUnscored").checked;
    const out = rows.filter((r) => {
      if (text && !`${r.title} ${r.url} ${r.explanation} ${r.domain}`.toLowerCase().includes(text)) return false;
      if (dom && r.domain !== dom) return false;
      if (kw && !String(r.user_keyword || "").split("|").map((k) => k.trim()).includes(kw)) return false;
      // Rows without a score, word count or date are kept only when "Include unscored / undated" is ticked.
      const s = num(r.quality_score);
      if (s === null) { if (!unscored) return false; }
      else if ((sMin !== null && s < sMin) || (sMax !== null && s > sMax)) return false;
      const w = num(r.word_count);
      if (w === null) { if (!unscored && (wMin !== null || wMax !== null)) return false; }
      else if ((wMin !== null && w < wMin) || (wMax !== null && w > wMax)) return false;
      const pd = r.publication_date ? new Date(r.publication_date) : null;
      if (!pd || isNaN(pd)) { if (!unscored) return false; }
      else if ((from && pd < from) || (to && pd > to)) return false;
      return true;
    });
    const { key, asc } = state.sort;
    const numeric = ["quality_score", "word_count", "search_position_best"].includes(key);
    out.sort((a, b) => {
      let x = a[key], y = b[key];
      if (numeric) { x = num(x); y = num(y); }
      if (x === null || x === "" || x === undefined) return 1;
      if (y === null || y === "" || y === undefined) return -1;
      const c = numeric ? x - y : String(x).localeCompare(String(y));
      return asc ? c : -c;
    });
    return out;
  }

  // ------------------------------------------------------------------ table
  function renderTable() {
    if (!state.scan) return;
    const rows = filteredRows();
    $("count").textContent = `${rows.length} article${rows.length === 1 ? "" : "s"}`;
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.sort === state.sort.key);
      th.classList.toggle("asc", th.dataset.sort === state.sort.key && state.sort.asc);
    });
    const tbody = $("table").querySelector("tbody");
    if (!rows.length) {
      tbody.replaceChildren(el("tr", {}, el("td", { colspan: "8", class: "muted", text: "No articles match this view and these filters." })));
      return;
    }
    tbody.replaceChildren(...rows.map((r) => el("tr", { tabindex: "0", onclick: (e) => { if (e.target.tagName !== "A") openDetail(r); },
      onkeydown: (e) => { if (e.key === "Enter") openDetail(r); } },
      el("td", { class: "num" }, scoreCell(r)),
      el("td", {}, el("span", { class: "cat " + catClass(r.quality_category), text: r.quality_category || "–" })),
      el("td", { class: "title" }, link(r.url, r.title || r.url), el("span", { class: "dom", text: [r.domain, r.user_keyword].filter(Boolean).join(" · ") })),
      el("td", {}, fmtLocal(r.publication_date) || el("span", { class: "muted", text: "unknown" }), el("div", { class: "muted small", text: r.date_confidence ? `${r.date_confidence} confidence` : "" })),
      el("td", { class: "num", text: r.word_count === "" ? "–" : String(r.word_count ?? "–") }),
      el("td", { text: r.article_type || "–" }),
      el("td", { class: "num", text: r.search_position_best === "" || r.search_position_best == null ? "–" : `#${r.search_position_best}` }),
      el("td", { class: "why", text: r.explanation || r.error || "" }),
    )));
  }

  // ------------------------------------------------------------------ detail dialog
  function openDetail(r) {
    const crit = (state.index.criteria || ["originality", "depth", "evidence", "usefulness", "readability"]);
    const signals = String(r.quality_signals || "").split("; ").filter(Boolean);
    const body = $("detailBody");
    body.replaceChildren(
      el("h2", { id: "dTitle", text: r.title || r.url }),
      el("p", {}, link(r.url)),
      el("div", { class: "grid2" },
        el("div", {},
          el("h3", { text: "Quality" }),
          el("p", {}, scoreCell(r), " ", el("span", { class: "cat " + catClass(r.quality_category), text: r.quality_category || "" }),
            r.article_type ? el("span", { class: "muted", text: ` · ${r.article_type}` }) : null),
          el("p", { text: r.explanation || "" }),
          isScored(r) ? el("div", {}, crit.map((c) => {
            const v = num(r[c + "_score"]);
            const max = Number(((state.scan.settings || {}).weights || {})[c]) || 20;
            return el("div", { class: "crit" }, el("span", { text: c }),
              el("span", { class: "bar" }, el("i", { style: `width:${v === null ? 0 : Math.min(100, (v / max) * 100)}%;background:var(--accent)` })),
              el("span", { class: "num", text: v === null ? "–" : `${v.toFixed(1)}/${max}` }));
          })) : null,
          signals.length ? el("h3", { text: "Signals (points lost)" }) : null,
          signals.length ? el("ul", { class: "signals" }, signals.map((s) => el("li", { text: s }))) : null,
          r.strengths ? el("p", {}, el("b", { text: "Strengths: " }), r.strengths) : null,
          r.scoring_notes ? el("p", { class: "muted", text: r.scoring_notes }) : null,
          r.llm_notes ? el("p", { class: "muted", text: r.llm_notes }) : null,
        ),
        el("div", {},
          el("h3", { text: "Date" }),
          el("dl", { class: "kv" },
            el("dt", { text: "Status" }), el("dd", { text: `${r.date_status || "–"} (${r.date_confidence || "–"} confidence)` }),
            el("dt", { text: "Published" }), el("dd", { text: r.publication_date ? `${fmtLocal(r.publication_date)} (${r.publication_date})` : "unknown" }),
            el("dt", { text: "Modified" }), el("dd", { text: r.modification_date || "–" }),
            el("dt", { text: "Flags" }), el("dd", { text: r.date_flags || "–" })),
          r.date_evidence ? el("pre", { class: "log", text: String(r.date_evidence).split(" | ").join("\n") }) : null,
          el("h3", { text: "News-search position" }),
          r.search_position_details
            ? el("div", {}, el("p", { text: `Best #${r.search_position_best || "–"}, average ${r.search_position_avg || "–"} over ${r.search_position_observations || 0} observation(s); ${r.search_result_page || ""}` }),
                el("pre", { class: "log", text: String(r.search_position_details).split("; ").join("\n") }),
                el("p", { class: "muted small", text: "Position in Google/Bing News RSS results at scan time – not a Google web-search ranking." }))
            : el("p", { class: "muted", text: "Not found via a relevance-ranked search source." }),
          el("h3", { text: "Discovery & extraction" }),
          el("dl", { class: "kv" },
            el("dt", { text: "Found via" }), el("dd", { text: r.discovery_source || "–" }),
            el("dt", { text: "Query" }), el("dd", { text: `${r.discovery_query || "–"}${r.query_origin ? ` (${r.query_origin})` : ""}` }),
            el("dt", { text: "Keyword" }), el("dd", { text: r.user_keyword || "–" }),
            el("dt", { text: "Author" }), el("dd", { text: r.author || "–" }),
            el("dt", { text: "Words / headings / paragraphs" }), el("dd", { text: `${r.word_count ?? "–"} / ${r.heading_count ?? "–"} / ${r.paragraph_count ?? "–"}` }),
            el("dt", { text: "External links" }), el("dd", { text: String(r.external_link_count ?? "–") }),
            el("dt", { text: "Canonical" }), el("dd", {}, r.canonical_url ? link(r.canonical_url) : "–"),
            el("dt", { text: "Similar to" }), el("dd", { text: r.similar_to || "–" }),
            el("dt", { text: "Extraction" }), el("dd", { text: `${r.extraction_status || "–"} (HTTP ${r.http_status || "–"}) ${r.error || ""}` })),
        ),
      ),
    );
    $("detail").showModal();
  }

  // ------------------------------------------------------------------ CSV download
  function csvCell(v) {
    if (v === null || v === undefined) return "";
    let s = Array.isArray(v) ? v.join("; ") : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // avoid spreadsheet formula injection from scraped text
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function downloadCSV(rows, name) {
    const cols = state.index.columns || Object.keys(rows[0] || {});
    const lines = [cols.join(",")].concat(rows.map((r) => cols.map((c) => csvCell(r[c])).join(",")));
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `${name}_scan${state.scan.scan_id}_${(state.scan.started_at || "").slice(0, 10)}.csv` });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  init();
})();
