import importlib.util
import json
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from scanner import live
from scanner.live import LiveSearchError, build_live_config, check_access_code, run_live_search

from .conftest import NOW, public_resolver
from .test_pipeline import mini_web


def test_config_validation_and_limits():
    cfg = build_live_config({"keywords": "technical seo, content marketing", "max_urls": 500, "window_hours": "72"})
    assert cfg["keywords"] == ["technical seo", "content marketing"]
    assert cfg["discovery"]["max_urls"] == 30 and cfg["discovery"]["window_hours"] == 72
    sources = cfg["discovery"]["search_sources"]
    assert sources["gdelt_doc"] and sources["bing_news_rss"] and not sources["google_news_rss"]  # follows config.yaml
    assert cfg["discovery"]["methods"]["seed_domains"] is False  # no domains given
    with pytest.raises(LiveSearchError):
        build_live_config({"keywords": []})
    with pytest.raises(LiveSearchError):
        build_live_config({"keywords": ["a", "b", "c", "d", "e", "f"]})
    with pytest.raises(LiveSearchError):
        build_live_config({"keywords": ["seo"], "window_hours": 999})
    with pytest.raises(LiveSearchError):
        build_live_config({"keywords": ["x" * 81]})


def test_access_code(monkeypatch):
    monkeypatch.delenv("SCAN_ACCESS_CODE", raising=False)
    assert check_access_code(None)
    monkeypatch.setenv("SCAN_ACCESS_CODE", "team-secret")
    assert check_access_code("team-secret")
    assert not check_access_code("wrong") and not check_access_code(None)


def live_base(base_config):
    base_config["topics"]["synonyms_file"] = None
    # The fixture mini-web serves a Google News feed; other sources get 404s from the fake session.
    base_config["discovery"]["search_sources"] = {"google_news_rss": True}
    return base_config


def test_live_search_returns_dashboard_payload(base_config):
    payload = run_live_search({"keywords": ["technical seo"], "competitor_domains": ["goodblog.com"], "max_urls": 20},
                              session=mini_web(), resolver=public_resolver, now=NOW, base_config=live_base(base_config))
    assert payload["live"] and not payload["partial"]
    urls = {r["url"].split("?")[0]: r for r in payload["rows"]}
    assert float(urls["https://thinblog.com/posts/ultimate-guide-technical-seo"]["quality_score"]) < 40
    assert urls["https://goodblog.com/blog/technical-seo-audit-large-sites"]["date_status"] == "confirmed_recent"
    assert payload["settings"]["keywords"] == ["technical seo"]
    assert payload["results"]["confirmed_recent"] >= 2


def test_live_search_respects_time_budget(base_config):
    payload = run_live_search({"keywords": ["technical seo"], "competitor_domains": ["goodblog.com"]},
                              session=mini_web(), resolver=public_resolver, now=NOW, budget_seconds=0,
                              base_config=live_base(base_config))
    assert payload["partial"] is True
    statuses = payload["discovery"]["search_sources"]
    assert statuses and all("time budget" in s["message"] for s in statuses)


def test_live_search_never_writes_project_data(base_config, tmp_path):
    before = set(Path("data").glob("*")) if Path("data").exists() else set()
    run_live_search({"keywords": ["technical seo"]}, session=mini_web(), resolver=public_resolver, now=NOW,
                    base_config=live_base(base_config))
    after = set(Path("data").glob("*")) if Path("data").exists() else set()
    assert after == before


@pytest.fixture
def api_server(monkeypatch, base_config):
    spec = importlib.util.spec_from_file_location("api_scan", Path(__file__).parent.parent / "api" / "scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = live.run_live_search
    monkeypatch.setattr(mod, "run_live_search", lambda params: real(
        params, session=mini_web(), resolver=public_resolver, now=NOW, base_config=live_base(base_config)))
    server = HTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/api/scan"
    server.shutdown()


def _post(url, body):
    req = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_endpoint(api_server, monkeypatch):
    monkeypatch.delenv("SCAN_ACCESS_CODE", raising=False)
    with urlopen(api_server) as r:
        info = json.loads(r.read())
    assert info["access_code_required"] is False and info["limits"]["max_urls"] == 30
    status, data = _post(api_server, {"keywords": ["technical seo"]})
    assert status == 200 and data["live"] and data["rows"]
    status, data = _post(api_server, {"keywords": []})
    assert status == 400 and "keyword" in data["error"]

    monkeypatch.setenv("SCAN_ACCESS_CODE", "s3cret")
    status, data = _post(api_server, {"keywords": ["technical seo"]})
    assert status == 401
    status, data = _post(api_server, {"keywords": ["technical seo"], "access_code": "s3cret"})
    assert status == 200


def test_live_search_blocks_private_competitor_domains(base_config):
    payload = run_live_search({"keywords": ["technical seo"], "competitor_domains": ["internal.example.com"]},
                              session=mini_web(), resolver=public_resolver, now=NOW, base_config=live_base(base_config))
    msgs = " ".join(" ".join(m["messages"]) for m in payload["discovery"]["methods"].values())
    assert "unsafe_url" in msgs  # SSRF guard applies to user-entered domains
