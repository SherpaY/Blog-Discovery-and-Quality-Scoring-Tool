import csv

from scanner.exporter import ALL_COLUMNS, EXPORT_COLUMNS, export_scan, select_rows, write_csv

ROWS = [
    {"url": "https://a.com/good", "title": "Good, with comma", "date_status": "confirmed_recent",
     "extraction_status": "ok", "quality_score": 82.0, "quality_flags": ["x", "y"]},
    {"url": "https://a.com/bad", "title": "Bad \"quoted\"", "date_status": "confirmed_recent",
     "extraction_status": "ok", "quality_score": 31.5},
    {"url": "https://a.com/unsure", "title": "Unsure", "date_status": "uncertain", "extraction_status": "ok",
     "quality_score": 20.0},
    {"url": "https://a.com/old", "title": "Old", "date_status": "outside_window", "extraction_status": "ok",
     "quality_score": 10.0},
    {"url": "https://a.com/fail", "title": "", "date_status": "uncertain", "extraction_status": "fetch_failed",
     "quality_score": "", "error": "timeout"},
    {"url": "https://a.com/js", "title": "", "date_status": "uncertain", "extraction_status": "js_rendered_suspected",
     "quality_score": ""},
    {"url": "https://a.com/skip", "title": "", "date_status": "", "extraction_status": "skipped_no_keyword_match"},
]


def test_required_columns_present_in_order():
    assert ALL_COLUMNS[:len(EXPORT_COLUMNS)] == EXPORT_COLUMNS
    for col in ("scan_date", "canonical_url", "date_confidence", "date_evidence", "quality_category",
                "originality_score", "readability_score", "quality_flags", "explanation", "http_status"):
        assert col in EXPORT_COLUMNS


def test_selection_rules():
    urls = lambda kind: [r["url"].split("/")[-1] for r in select_rows(ROWS, kind, 60)]  # noqa: E731
    assert urls("confirmed_recent") == ["good", "bad"]
    assert urls("low_quality") == ["bad"]  # uncertain-date and out-of-window articles are never "confirmed"
    assert urls("needs_date_review") == ["unsure"]
    assert urls("failed_extractions") == ["fail", "js"]
    assert len(select_rows(ROWS, "all_articles", 60)) == len(ROWS)


def test_csv_round_trip_with_special_characters(tmp_path):
    path = write_csv(ROWS, tmp_path / "x.csv")
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == ALL_COLUMNS
    assert rows[0]["title"] == "Good, with comma" and rows[1]["title"] == 'Bad "quoted"'
    assert rows[0]["quality_flags"] == "x; y"
    assert rows[1]["quality_score"] == "31.5"


def test_export_scan_writes_five_dated_files(tmp_path):
    out = export_scan(ROWS, tmp_path, 60, scan_id=7)
    assert set(out) == {"all_articles", "confirmed_recent", "low_quality", "needs_date_review", "failed_extractions"}
    for kind, path in out.items():
        assert path.exists() and path.name.startswith(kind + "_") and path.name.endswith("_scan7.csv")
        assert path.parent.parent == tmp_path  # output/<YYYY-MM-DD>/
    with out["low_quality"].open(encoding="utf-8-sig") as fh:
        assert len(list(csv.DictReader(fh))) == 1
