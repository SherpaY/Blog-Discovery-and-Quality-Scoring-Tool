from datetime import datetime, timezone

from scanner.http_client import FetchResult
from scanner.storage import Storage

from .conftest import NOW


def test_scan_lifecycle_and_history(tmp_path):
    db = tmp_path / "t.db"
    s = Storage(db)
    sid = s.create_scan({"keywords": ["seo"]}, "manual", 168, NOW)
    assert s.latest_resumable_scan()["id"] == sid
    s.finish_scan(sid, "completed", {"results": {"confirmed_recent": 3}})
    s.close()
    s2 = Storage(db)  # persisted on disk
    scan = s2.get_scan(sid)
    assert scan["status"] == "completed" and scan["summary"]["results"]["confirmed_recent"] == 3
    assert scan["config"]["keywords"] == ["seo"]
    assert s2.latest_resumable_scan() is None
    assert [x["id"] for x in s2.list_scans()] == [sid]


def test_queue_supports_resume(tmp_path):
    s = Storage(tmp_path / "t.db")
    sid = s.create_scan({}, "manual", 48, NOW)
    s.enqueue(sid, [("https://a.com/p1", {"url": "https://a.com/p1"}), ("https://a.com/p2", {"url": "https://a.com/p2"})])
    s.enqueue(sid, [("https://a.com/p1", {"url": "dup"})])  # duplicates ignored
    s.mark_queue(sid, "https://a.com/p1", "done")
    pending = s.queue_items(sid, "pending")
    assert [p[0] for p in pending] == ["https://a.com/p2"]
    assert len(s.queue_items(sid)) == 2
    assert s.mark_stale_running_as_interrupted() == 1
    assert s.latest_resumable_scan()["status"] == "interrupted"


def test_articles_seen_urls_and_texts(tmp_path):
    s = Storage(tmp_path / "t.db")
    sid = s.create_scan({}, "manual", 48, NOW)
    s.save_article(sid, "https://a.com/p1", {"url": "https://a.com/p1", "canonical_url": "https://a.com/p1",
                                             "extraction_status": "ok", "quality_score": 72.5}, "body text here")
    s.save_article(sid, "https://a.com/p2", {"url": "https://a.com/p2", "extraction_status": "fetch_failed"})
    assert s.is_seen("https://a.com/p1")
    assert not s.is_seen("https://a.com/p2")  # failed fetches can be retried next time
    assert not s.is_seen("https://a.com/never")
    assert s.seen_canonical("https://a.com/p1", exclude="https://other.com/x")
    assert [r["url"] for r in s.articles_for_scan(sid)] == ["https://a.com/p1", "https://a.com/p2"]
    s.update_article_row(sid, "https://a.com/p1", {"url": "https://a.com/p1", "quality_score": 40})
    assert s.articles_for_scan(sid)[0]["quality_score"] == 40
    assert s.recent_texts(7, exclude_urls=set()) == [("https://a.com/p1", "body text here")]
    assert s.recent_texts(7, exclude_urls={"https://a.com/p1"}) == []


def test_http_cache_ttl(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.put(FetchResult(url="https://a.com/x", final_url="https://a.com/x", status=200, content=b"hello",
                      headers={"content-type": "text/html", "set-cookie": "secret"}))
    hit = s.get("https://a.com/x", 3600)
    assert hit.content == b"hello" and hit.status == 200
    assert "set-cookie" not in hit.headers  # only safe headers cached
    assert s.get("https://a.com/x", 0) is None
    assert s.get("https://a.com/missing", 3600) is None


def test_timestamps_stored_in_utc(tmp_path):
    s = Storage(tmp_path / "t.db")
    local = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc).astimezone()
    sid = s.create_scan({}, "manual", 24, local)
    assert s.get_scan(sid)["reference_now"] == "2026-10-11T14:00:00Z"
