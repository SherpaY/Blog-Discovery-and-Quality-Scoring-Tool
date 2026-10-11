import pytest
import requests

from scanner.safety import UnsafeURLError, check_url, is_public_ip, is_safe_url

from .conftest import FakeResponse, FakeSession, public_resolver


# ----------------------------------------------------------------------------- SSRF checks
@pytest.mark.parametrize("url", [
    "http://localhost/admin",
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://10.1.2.3/",
    "http://192.168.0.1/router",
    "http://172.16.5.4/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",
    "http://0.0.0.0/",
    "http://internal.example.com/",   # resolves to 10.0.0.5
    "http://metadata.test/",          # resolves to link-local
    "http://loopback.test/",
    "http://mixed.test/",             # one public + one private address -> blocked
    "http://nxdomain.test/",          # DNS failure
    "http://printer.local/",
    "file:///etc/passwd",
    "ftp://example.com/file",
    "gopher://example.com/",
    "http://user:pass@example.com/",
    "http://example.com:22/",
    "http://2130706433/",             # decimal form of 127.0.0.1
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        check_url(url, public_resolver)


def test_public_urls_allowed():
    assert is_safe_url("https://goodblog.com/blog/post", public_resolver)
    assert is_safe_url("http://93.184.216.34/", public_resolver)
    assert is_public_ip("8.8.8.8") and not is_public_ip("::ffff:127.0.0.1")


def test_redirect_to_private_address_is_blocked(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", b"", status=404)
    s.add("https://goodblog.com/go", b"", status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    res = fetcher_factory(s).fetch("https://goodblog.com/go")
    assert not res.ok and res.error.startswith("unsafe_redirect")
    assert "http://169.254.169.254/latest/meta-data/" not in s.calls


def test_redirect_to_localhost_name_blocked(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", b"", status=404)
    s.add("https://goodblog.com/go", b"", status=301, headers={"Location": "http://localhost:8080/"})
    assert fetcher_factory(s).fetch("https://goodblog.com/go").error.startswith("unsafe_redirect")


def test_safe_redirect_followed_and_final_url_recorded(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", b"", status=404)
    s.add("https://goodblog.com/old", b"", status=301, headers={"Location": "/new-post-url"})
    s.add("https://goodblog.com/new-post-url", b"<html>ok</html>", content_type="text/html")
    res = fetcher_factory(s).fetch("https://goodblog.com/old")
    assert res.ok and res.final_url == "https://goodblog.com/new-post-url"
    assert res.redirects == ["https://goodblog.com/old"]


def test_redirect_loop_stops(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", b"", status=404)
    s.add("https://goodblog.com/a", b"", status=302, headers={"Location": "/b"})
    s.add("https://goodblog.com/b", b"", status=302, headers={"Location": "/a"})
    assert fetcher_factory(s, max_redirects=3).fetch("https://goodblog.com/a").error == "too_many_redirects"


# ----------------------------------------------------------------------------- failures & timeouts
def test_timeout_is_retried_once_then_reported(fetcher_factory):
    s = FakeSession()
    s.add("https://slow.com/robots.txt", b"", status=404)
    s.routes["https://slow.com/post-slug"] = requests.Timeout("read timed out")
    res = fetcher_factory(s).fetch("https://slow.com/post-slug")
    assert res.error == "timeout" and not res.ok
    assert s.calls.count("https://slow.com/post-slug") == 2  # one retry, not infinite


def test_connection_error_and_http_errors(fetcher_factory):
    s = FakeSession()
    s.add("https://down.com/robots.txt", b"", status=404)
    s.routes["https://down.com/x-post"] = requests.ConnectionError("refused")
    s.add("https://down.com/missing-post", b"nope", status=404)
    f = fetcher_factory(s)
    assert f.fetch("https://down.com/x-post").error.startswith("connection_error")
    res = f.fetch("https://down.com/missing-post")
    assert res.status == 404 and res.error == "http_404"


def test_server_error_retried_once(fetcher_factory):
    s = FakeSession()
    s.add("https://flaky.com/robots.txt", b"", status=404)
    responses = iter([FakeResponse(503, b"busy"), FakeResponse(200, b"<html>ok</html>", {"Content-Type": "text/html"})])
    s.routes["https://flaky.com/post-a"] = lambda url: next(responses)
    assert fetcher_factory(s).fetch("https://flaky.com/post-a").ok


def test_response_size_limit(fetcher_factory):
    s = FakeSession()
    s.add("https://big.com/robots.txt", b"", status=404)
    s.add("https://big.com/huge-post", b"x" * 5000, content_type="text/html")
    res = fetcher_factory(s, max_page_bytes=1000).fetch("https://big.com/huge-post")
    assert res.error.startswith("too_large")


# ----------------------------------------------------------------------------- robots.txt
ROBOTS = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 1\n\nUser-agent: BlockedScanner\nDisallow: /\n"


def test_robots_disallow_respected(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", ROBOTS, content_type="text/plain")
    s.add("https://goodblog.com/private/secret-post", b"<html>secret</html>")
    f = fetcher_factory(s)
    res = f.fetch("https://goodblog.com/private/secret-post")
    assert res.error.startswith("robots") and "https://goodblog.com/private/secret-post" not in s.calls
    assert f.is_allowed("https://goodblog.com/blog/ok-post")[0]
    assert f.crawl_delay("https://goodblog.com/x") == 1.0


def test_robots_user_agent_specific_block(fetcher_factory):
    s = FakeSession()
    s.add("https://goodblog.com/robots.txt", ROBOTS, content_type="text/plain")
    f = fetcher_factory(s, user_agent="BlockedScanner/1.0")
    assert not f.is_allowed("https://goodblog.com/blog/ok-post")[0]


def test_robots_missing_allows_and_unreachable_disallows(fetcher_factory):
    s = FakeSession()
    s.add("https://norobots.com/robots.txt", b"", status=404)
    s.add("https://broken.com/robots.txt", b"", status=503)
    f = fetcher_factory(s, max_retries=0)
    assert f.is_allowed("https://norobots.com/post-a")[0]
    allowed, reason = f.is_allowed("https://broken.com/post-a")
    assert not allowed and "unreachable" in reason


def test_excessive_crawl_delay_skips_domain(fetcher_factory):
    s = FakeSession()
    s.add("https://slowsite.com/robots.txt", b"User-agent: *\nCrawl-delay: 120\n")
    res = fetcher_factory(s, max_crawl_delay_seconds=30).fetch("https://slowsite.com/post-a")
    assert res.error.startswith("robots: crawl-delay")


def test_per_domain_rate_limit_waits():
    from scanner.http_client import Fetcher

    s = FakeSession()
    s.add("https://rl.com/robots.txt", b"", status=404)
    s.add("https://rl.com/a-post", b"<html>a</html>")
    s.add("https://rl.com/b-post", b"<html>b</html>")
    sleeps = []
    clock = iter([0.0, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
    f = Fetcher({"min_delay_seconds": 2, "cache_ttl_hours": 0}, session=s, resolver=public_resolver,
                sleep=sleeps.append, clock=lambda: next(clock))
    f.fetch("https://rl.com/a-post")
    f.fetch("https://rl.com/b-post")
    assert sleeps and all(x > 1.0 for x in sleeps)
