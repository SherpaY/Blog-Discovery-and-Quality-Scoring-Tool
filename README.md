# Blog Quality Scanner

A free, local tool that finds blog articles published recently (last 24 h, 48 h, 72 h or 7 days)
about **your keywords**, checks when they were really published, scores their content quality
with transparent rules, and exports the results to CSV.

* **Zero cost.** No paid APIs, no accounts, no API keys, no cloud. Everything runs on your Mac.
* **Explainable.** Every score comes with the exact signals that caused it.
* **Honest.** Free discovery sources only show part of the web. When a source is blocked, the tool
  says so; it never invents results, rankings or keyword metrics.

---

## 1. How it works (in plain language)

1. **You give it keywords** (e.g. `technical seo`) and, optionally, competitor websites.
2. **It builds search phrases.** Your keywords, plus clearly labelled variations such as
   `technical seo tips` or word combinations. No search volumes or difficulty scores are invented.
3. **It finds candidate articles** from several free sources. Each source works on its own, so if
   one fails, the rest continue:
   * **Keyword search feeds:** Google News RSS, Bing News RSS, Medium and DEV tag feeds.
   * **Competitor sites:** their RSS feeds and XML sitemaps, discovered automatically.
   * **Any extra feeds or sitemaps** you add yourself.
4. **It throws out obvious non-articles and old posts** before downloading anything. That means
   tag pages, category pages, author pages, pagination and logins, plus posts whose feed or
   sitemap date is too old.
5. **It downloads each article politely.** It obeys robots.txt and waits between requests to the
   same site. Only public web addresses are allowed.
6. **It extracts the article.** Title, author, headings, body text, links and every date it can
   find. Menus, footers, cookie banners, ads and "related posts" are removed.
7. **It verifies the publication date.** It compares dates from the article's structured data,
   meta tags, the feed, the sitemap and the visible page text, then decides:
   * **Confirmed recent:** a reliable publication date inside your window.
   * **Needs date review:** the date is missing, conflicting, low-confidence, or only a
     "last modified" date is available.
   * **Outside window:** an older article, including old posts that were merely updated recently.
8. **It scores quality from 0 to 100** using five rule-based criteria (section 6).
9. **It saves everything** in a local SQLite database (so URLs aren't reprocessed next time) and
   writes five CSV files.

---

## 2. Install on your Mac (one time, about 5 minutes)

You need **Python 3.10 or newer**. Check in Terminal:

```bash
python3 --version
```

If it is missing or older, install it from <https://www.python.org/downloads/macos/> (free), or with
Homebrew: `brew install python@3.12`.

Then, in Terminal:

```bash
# 1. Get the project (or download the ZIP from GitHub and unzip it)
git clone https://github.com/SherpaY/Blog-Discovery-and-Quality-Scoring-Tool.git
cd Blog-Discovery-and-Quality-Scoring-Tool

# 2. Create a private Python environment for this project
python3 -m venv .venv
source .venv/bin/activate

# 3. Install the free dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Check everything works (no internet needed for the tests)
python -m pytest
```

You should see something like `105 passed, 3 skipped`.

---

## 3. Using the web app

```bash
cd Blog-Discovery-and-Quality-Scoring-Tool
source .venv/bin/activate
streamlit run app.py          # or: scripts/start_app.sh
```

Your browser opens <http://localhost:8501>. The app only runs on your own computer.

**Sidebar (settings, saved automatically to `data/user_config.yaml`):**

* **Keywords / topics:** one per line. These drive discovery.
* **Competitor domains** (optional): e.g. `example.com` or `example.com/blog`.
* **Discovery period:** last 24 h, 48 h, 72 h or 7 days.
* **Maximum URLs to process:** limits how many articles are downloaded per scan.
* **Flag articles scoring below:** the low-quality threshold (default 60).
* **Only keep articles that mention my keywords:** on by default.
* **Discovery methods and search sources:** switch each on or off.
* Press **Save settings**.

**Scan tab:** press **Start scan**. Progress updates live. **Stop scan** stops safely and keeps
progress, and **Resume interrupted scan** continues where it stopped. The discovery report shows
which sources worked, which were blocked and why, and which queries were used.

**Results tab:**

* Choose a view: all articles, confirmed recent, potentially low quality, date review queue, or
  failed extractions.
* Filter by domain, keyword, word count, quality score and publication date. Dates are shown in
  your Mac's local time.
* Click any URL to open it in your browser.
* Download the shown rows, or only the flagged ones, as CSV.
* Pick an article under **Article details** to see every scoring signal, the date evidence, and its
  news-search positions.

**Tools & schedule tab:** find a site's feeds and sitemaps, test which search sources work right
now, preview keyword variations, and install the daily schedule.

---

## 4. Using the command line

```bash
source .venv/bin/activate
python -m scanner scan                                  # scan with saved settings
python -m scanner scan --keywords "technical seo" "content marketing" --window 168 --max-urls 80
python -m scanner scan --resume                         # continue an interrupted scan
python -m scanner stop                                  # ask a running scan to stop safely
python -m scanner status                                # live status of the current/last scan
python -m scanner history                               # previous scans with counts
python -m scanner discover-feeds example.com            # feed + sitemap finder
python -m scanner probe-search                          # which free search sources work now
python -m scanner expand "technical seo audit"          # preview generated query variations
```

Every scan prints a summary: URLs discovered, queued, rejected (with reasons), confirmed recent,
flagged, needing date review, outside the window, and failed.

---

## 5. Output files

Each scan writes five CSVs into `output/<date>/`, for example
`output/2026-10-11/low_quality_20261011_0730_scan12.csv`:

| File | Contains |
|---|---|
| `all_articles_*` | Every URL processed in the scan, including skipped and failed ones |
| `confirmed_recent_*` | Articles with a reliable publication date inside the window |
| `low_quality_*` | Confirmed-recent articles scoring below your threshold |
| `needs_date_review_*` | Articles whose date is uncertain, conflicting or modification-only |
| `failed_extractions_*` | Pages that could not be fetched or read (with the reason) |

**Columns:** `scan_date, title, url, canonical_url, domain, user_keyword, discovery_query,
discovery_source, publication_date, modification_date, date_confidence, date_evidence, author,
article_type, word_count, heading_count, paragraph_count, external_link_count, quality_score,
quality_category, originality_score, depth_score, evidence_score, usefulness_score,
readability_score, quality_flags, explanation, extraction_status, http_status`.

These are followed by extra transparency columns: `search_position_best`, `search_position_avg`,
`search_result_page`, `search_position_observations`, `search_position_details`, `date_status`,
`date_flags`, `publication_date_local`, `query_origin` (user keyword vs generated variation),
`quality_signals` (each rule and the points it cost), `strengths`, `similar_to`, `scoring_notes`,
`extraction_notes`, `error`, `llm_notes`, `scan_id`.

All timestamps in the CSVs are **UTC** (ending in `Z`). `publication_date_local` and the web app
show your local time. Files are UTF-8 and open correctly in Excel and Numbers.

---

## 6. How quality is scored

Each article starts with 100 points split across five criteria. Rules remove part of a criterion's
points, and every lost point is listed with its reason.

| Criterion (default points) | Example signals |
|---|---|
| **Originality & distinctiveness (25)** | Near-copy of another collected article (TF-IDF similarity plus copied-passage overlap); repeated paragraphs or headings; heavy boilerplate phrasing ("in today's fast-paced world"); one phrase repeated unnaturally; repetitive vocabulary |
| **Depth & coverage (20)** | Much shorter than typical *for its type*; many headings with almost no text under them; little subject-specific vocabulary; title targets your keyword but the body never discusses it |
| **Evidence & support (20)** | No external sources; broad claims ("studies show", "experts say") without links; no data, quotes or attribution. **Weighted by article type**: matters most for news, least for opinion |
| **Specificity & usefulness (20)** | Few concrete details per 100 words (numbers, distinct names, examples, code, tables); vague wording; a tutorial with no steps; a "25 tips" title with only 8 tips; a review with no pros/cons, pricing or testing |
| **Structure & readability (15)** | Very long sentences or paragraphs; long article with no subheadings; disorganised heading levels; low Flesch reading ease |

Article types (news, tutorial, listicle, review, opinion, general) are detected from structured
data, the title and the URL, and each has its own expected length and evidence importance.

**Categories (provisional, not universal standards):** 0–39 *Potentially very low quality*,
40–59 *Needs manual review*, 60–79 *Reasonable quality*, 80–100 *Strong quality signals*.

**Safeguards built in:**

* It never labels anything as AI-generated.
* If an article lands in the lowest band but all its problems come from a single criterion, it is
  shown as *Needs manual review* instead. One signal alone is not proof.
* Short articles that are specific and cite sources have their length penalty halved.
* Pages with too little extractable text (for example, sites that need JavaScript) are marked
  *Not scored* instead of receiving a low score.
* The explanation only ever repeats signals that were actually detected.
* All weights, thresholds, categories and article-type settings are in `config.yaml`.

**Optional local AI notes:** if you install [Ollama](https://ollama.com) (free) and set
`ollama.enabled: true`, flagged articles get a short model opinion in `llm_notes`. It never changes
the score, and the tool works fully without it.

---

## 7. Search positions (what they are, and are not)

For articles found through **Google News RSS** or **Bing News RSS**, the tool records where the
article appeared in those results for your query at the time of the scan:

* `search_position_best`: best position in this scan.
* `search_position_avg`: average across every scan that has seen the article (history is kept in
  SQLite, so you can watch it change).
* `search_result_page`: the equivalent results page, assuming 10 results per page.
* `search_position_details`: source, query, position and timestamp of each observation.

**These are news-search feed positions, not Google web-search rankings.** Real Google web rankings
cannot be collected for free without breaking Google's terms:

* Scraping Google's result pages is prohibited, and protected by CAPTCHAs this tool will not bypass.
* Google's Custom Search JSON API is closed to new customers and shuts down on 1 January 2027.
* Bing's Web Search API was retired in 2025.
* Search Console only covers sites you own.

Getting true web positions for competitors' articles requires a paid SERP or rank-tracking service.
Medium and DEV tag feeds are sorted newest-first, not by relevance, so no position is recorded for
them.

---

## 8. Daily automatic scans (macOS)

From the web app's **Tools & schedule** tab, or from Terminal:

```bash
source .venv/bin/activate
python -m scanner schedule install --hour 7 --minute 30   # every day at 07:30
python -m scanner schedule status
python -m scanner schedule uninstall
```

This uses macOS's built-in **launchd**. A job file is written to
`~/Library/LaunchAgents/com.blogqualityscanner.daily.plist`.

* **Your Mac must be on and you must be logged in.** If it is asleep at the scheduled time, the
  scan runs when it wakes. If it is shut down, that day's run is skipped. No cloud service runs it
  for you.
* **Overlapping scans are impossible.** A lock file means a scheduled run will not start while
  another scan is running.
* **Interrupted scans resume.** If a scan was interrupted (sleep, crash, power loss), the next
  scheduled run within 20 hours continues it instead of starting over.
* **Logs:** `logs/scanner_YYYYMMDD.log`, warnings and errors in `logs/errors.log`, and launchd
  output in `logs/launchd.*.log`.
* **Where the data goes:** CSVs land in dated folders under `output/`, and history is in
  `data/scanner.db`.

If you move the project folder or recreate `.venv`, run `schedule install` again.

---

## 9. Discovery sources: evaluation and limits

| Source | Cost / account | Status | Limits |
|---|---|---|---|
| Google News RSS (`news.google.com/rss/search`) | Free, none | Used if robots.txt allows; probed each scan | Only news-indexed sites. Some links are opaque Google redirects, which are resolved by matching the title in the publisher's own feed, or skipped and reported. Undocumented, may change or rate-limit |
| Bing News RSS (`bing.com/news/search?format=rss`) | Free, none | Same | Only news-indexed sites. Undocumented, may change |
| Medium tag feeds (`medium.com/feed/tag/<tag>`) | Free, none | Documented by Medium | Medium only, single-tag match, member-only posts show a preview |
| DEV tag feeds (`dev.to/feed/tag/<tag>`) | Free, none | Probed each scan | dev.to only, software topics |
| WordPress.com tag feeds | Free, none | Off by default (experimental) | Format not officially documented |
| Hacker News (Algolia API) | Free, none | Off by default | Only links people submitted; submission time is not publication time |
| Competitor RSS/Atom feeds | Free | Main method | Feeds usually list only the last 10–50 posts |
| Competitor XML sitemaps | Free | Main method | `lastmod` is only a hint; undated sitemap entries are skipped; very large sitemaps are capped |
| **Google / Bing / DuckDuckGo web result pages** | — | **Not used** | Terms prohibit automated queries, and CAPTCHAs and anti-bot systems must not be bypassed |

Every search source is tested at the start of each scan. If it is blocked (403), rate-limited (429),
disallowed by robots.txt, or returns a CAPTCHA or consent page instead of a feed, it is **disabled
for that scan and the reason is shown**. Nothing is simulated.

> These sources were evaluated from their documentation and tested against saved samples. They could
> not be checked live while this tool was built, because the build environment had no general
> internet access. Run `python -m scanner probe-search` on your Mac to see what works for you today.

---

## 10. Limitations: data accuracy and other caveats

**Coverage**

* No free method can find *every* article published on the web. Results cover only what these feeds
  and sitemaps expose.
* Many blogs are not in Google News or Bing News, so they are found only if you list them as
  competitor domains or add their feeds.
* Competitor feeds often hold only the latest 10–50 posts, so a very busy site may have more posts
  in the window than the feed shows. Sitemaps help, but only when they include dates.
* The keyword filter matches words, not meaning. An article about "site audits" may be missed for
  the keyword "technical seo", and an off-topic article that happens to use those words may be kept.
* Search sources can change, rate-limit or block automated access at any time.

**Publication dates**

* Dates come from what the site publishes. If a site puts the wrong date in its metadata, the tool
  repeats that mistake. Disagreements between sources are flagged, not resolved by guessing.
* Sites that show no publication date, or only a "last updated" date, land in the date review queue.
  They are never counted as confirmed recent.
* Date-only values (no time) and dates without a timezone are less precise. Articles near the edge of
  the window go to review instead of being confirmed.
* Sitemap `lastmod` and HTTP `Last-Modified` are used only as hints.
* News-search dates (Google/Bing) are when the item was indexed or published as reported by the
  engine. They are cross-checked against the article itself.

**Quality scores**

* Scores are **screening signals for manual review**, not judgements of truth or value. A clear,
  plainly written expert article can score lower than it deserves, and a polished but empty article
  can occasionally score well.
* The tool does **not fact-check**. It can see that a statistic is present, not whether it is
  correct.
* Originality is measured **only against articles this tool has collected** (this scan and the last
  7 days), never the whole internet. When two collected articles are near-identical, the tool cannot
  tell which one was first.
* Thresholds were set by hand and are tuned for English. Other languages score unreliably.
* Article-type detection is heuristic. An article misclassified as a tutorial may be judged against
  the wrong expected length.
* Heavy JavaScript sites (content loaded in the browser) cannot be read without a rendering browser,
  and are reported as `js_rendered_suspected` rather than scored.
* Text extraction can occasionally include leftover page elements or miss content inside unusual
  layouts. Word counts are approximate.

**Search positions and keywords**

* Positions are from **news-search feeds at scan time**, not Google web rankings (section 7). They
  vary by location, language and time, and the feeds use US-English settings.
* Keyword variations are generated text, not keyword research. **No search volume, difficulty,
  traffic or intent data exists in this tool.**

**Operational**

* Polite crawling (robots.txt, at least a 2-second gap per site) makes large scans slow by design.
  Sites asking for more than 30 seconds between requests are skipped.
* Sites behind logins, paywalls, CAPTCHAs or bot protection are skipped and reported. They are never
  bypassed.
* Daily scans need the Mac on and logged in.
* Pages are cached for 6 hours. Already-processed URLs are skipped in later scans unless you enable
  *Re-process URLs*.
* Security: only public http/https addresses are fetched, and every redirect is re-checked. A
  theoretical DNS-rebinding gap remains (an address is checked, then resolved again when fetched),
  which is acceptable for a local research tool.

**What would need a paid service**

* True Google web-search rankings and SERP features (paid SERP API or rank tracker).
* Search volume, keyword difficulty, traffic estimates and backlink data (Semrush, Ahrefs and
  similar).
* Web-wide originality or plagiarism checks (paid plagiarism APIs).
* Rendering JavaScript-heavy pages at scale (paid browser services). Though this could be added for
  free locally with Playwright if needed.
* Running scans while your Mac is off (paid hosting).

---

## 11. Configuration reference

Defaults live in `config.yaml`, and your changes from the app are saved to `data/user_config.yaml`
(not committed). Key settings:

* `discovery.window_hours`: 24, 48, 72 or 168.
* `discovery.max_urls` and `max_urls_per_domain`: limits on articles fetched.
* `discovery.filter_by_keywords`: keep only articles that mention a keyword.
* `discovery.search_sources.*`: switch individual search sources on or off.
* `topics.variation_templates`: e.g. `"{kw} guide"`.
* `topics.synonyms_file`: copy `data/synonyms.example.yaml` to `data/synonyms.yaml` to add synonyms.
* `http.min_delay_seconds`, `timeout_seconds`, `max_retries`, `respect_robots`: crawling behaviour.
* `dates.min_confidence_for_confirmed`: `medium` by default; use `high` to be stricter.
* `quality.weights`, `quality.thresholds`, `quality.categories`, `quality.article_types`: all
  scoring knobs.
* `quality.similarity.method`: `tfidf` (default), or `semantic` after
  `pip install sentence-transformers` (free, local, a large download).

---

## 12. Project structure

```
app.py                   Streamlit web interface
config.yaml              default settings (edit freely)
requirements.txt
scanner/
  cli.py                 command line (python -m scanner ...)
  pipeline.py            scan orchestration, lock, stop/resume, summary
  discovery.py           runs all discovery methods, merges, filters
  rss_discovery.py       Method A: RSS/Atom parsing, pagination, feed finder
  sitemap_discovery.py   Method B: robots.txt, sitemap indexes, news sitemaps
  search_discovery.py    Method C: free keyword search feeds + positions
  topics.py              keyword variations (labelled, no fake metrics)
  http_client.py         polite fetching: robots.txt, rate limits, retries, cache
  safety.py              SSRF protection (public IPs only, every redirect checked)
  extractor.py           article content and metadata extraction
  date_verification.py   publication-date evidence and confidence
  quality_scoring.py     transparent 0-100 scoring
  similarity.py          TF-IDF / copied-passage similarity
  storage.py             SQLite: scans, queue, articles, positions, cache
  exporter.py            CSV exports
  scheduler.py           macOS launchd daily job
  llm_review.py          optional Ollama notes
tests/                   offline tests with saved HTML/RSS/sitemap fixtures
scripts/                 start_app.sh, run_scan.sh
data/ output/ logs/      local only - never committed
```

## 13. Tests

```bash
python -m pytest                       # full offline suite (no internet used)
RUN_LIVE_TESTS=1 python -m pytest tests/test_integration_live.py -v -s   # optional live check
```

The offline suite covers:

* RSS/Atom parsing and pagination.
* Sitemap indexes, gzip, news sitemaps, and malformed or oversized sitemaps.
* URL normalisation and deduplication.
* Date parsing, timezones, and conflicting or modified-only dates.
* HTML extraction and quality scoring.
* CSV export and SQLite persistence.
* Timeouts, retries and failed requests.
* robots.txt handling.
* SSRF and unsafe-redirect blocking.
* Search-source blocking.
* A full end-to-end scan over a simulated set of websites with known dates and deliberately varied
  quality, including stop/resume and overlap prevention.

## Responsible use

Use this tool for research on publicly available pages. It identifies itself with a descriptive
User-Agent, honours robots.txt and crawl delays, rate-limits per site, and never bypasses logins,
paywalls or anti-bot measures. Respect each website's terms of use.
