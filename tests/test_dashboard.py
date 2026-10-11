import json

from scanner.dashboard import publish, slim_database
from scanner.exporter import ALL_COLUMNS
from scanner.pipeline import ScanRunner
from scanner.storage import Storage

from .conftest import NOW, public_resolver
from .test_pipeline import mini_web


def test_publish_writes_index_and_scan_files(scan_config, tmp_path):
    result = ScanRunner(scan_config, session=mini_web(), resolver=public_resolver, now=NOW).run()
    db = scan_config["paths"]["data_dir"] + "/scanner.db"
    out = tmp_path / "site_data"
    index = publish(db, out)
    assert index["columns"] == ALL_COLUMNS and len(index["scans"]) == 1
    entry = index["scans"][0]
    assert entry["settings"]["keywords"] == ["technical seo", "content marketing"]
    assert entry["results"]["confirmed_recent"] == result["results"]["confirmed_recent"]
    payload = json.loads((out / entry["file"]).read_text())
    assert payload["rows"] and set(payload["rows"][0]) == set(ALL_COLUMNS)
    assert not any(k.startswith("_") for r in payload["rows"] for k in r)  # internal article blobs not published
    assert payload["discovery"]["search_sources"][0]["available"]

    # History survives losing the database (e.g. CI cache eviction): old entries stay in the index.
    index2 = publish(tmp_path / "fresh.db", out)
    assert [e["file"] for e in index2["scans"]] == [entry["file"]]

    # Pruning keeps only the newest N scans and deletes old files.
    fake = dict(entry, file="scans/scan_200001010000_99.json", started_at="2000-01-01T00:00:00Z")
    (out / fake["file"]).write_text("{}")
    idx = json.loads((out / "index.json").read_text())
    idx["scans"].append(fake)
    (out / "index.json").write_text(json.dumps(idx))
    index3 = publish(tmp_path / "fresh.db", out, keep=1)
    assert [e["file"] for e in index3["scans"]] == [entry["file"]]
    assert not (out / fake["file"]).exists()


def test_slim_database_drops_http_cache(scan_config):
    ScanRunner(scan_config, session=mini_web(), resolver=public_resolver, now=NOW).run()
    db = scan_config["paths"]["data_dir"] + "/scanner.db"
    s = Storage(db)
    assert s.conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] > 0
    s.close()
    slim_database(db)
    s = Storage(db)
    assert s.conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0
    assert s.list_scans()  # scan history kept
