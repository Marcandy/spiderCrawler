import httpx
import pytest

from spidercrawler.config import Config
from spidercrawler.fetcher import (
    Fetcher,
    FetchError,
    RequestCapError,
    RobotsDisallowedError,
)
from spidercrawler.records import PageValidators

CONFIG = Config.model_validate(
    {
        "base_url": "https://www.pacourts.us/",
        "contact_email": "maintainer@example.org",
        "limits": {"min_delay_seconds": 5, "max_requests_per_run": 30},
        "sources": [],
    }
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make_fetcher(routes, config=CONFIG):
    """routes: {url: httpx.Response or callable(request) -> Response}. Unknown URLs 404."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        route = routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        return route(request) if callable(route) else route

    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(config, client=client, clock=clock, sleep=clock.sleep)
    return fetcher, seen, clock


def text(body, status=200, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/plain", **headers})


def html(body="<html></html>", status=200, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html", **headers})


def redirect(location, status=302):
    return httpx.Response(status, headers={"location": location})


PAGE = "https://www.pacourts.us/courts/page"


def test_missing_robots_allows_and_identifies_itself():
    fetcher, seen, _ = make_fetcher({PAGE: html("ok")})
    result = fetcher.get(PAGE)
    assert result.status == 200
    assert result.content == b"ok"
    assert all("maintainer@example.org" in r.headers["user-agent"] for r in seen)
    assert [str(r.url) for r in seen] == ["https://www.pacourts.us/robots.txt", PAGE]


def test_robots_rules_are_applied():
    robots = "User-agent: *\nDisallow: /courts/\n"
    fetcher, seen, _ = make_fetcher({"https://www.pacourts.us/robots.txt": text(robots)})
    with pytest.raises(RobotsDisallowedError):
        fetcher.get(PAGE)
    assert len(seen) == 1  # only robots.txt was requested


def test_robots_is_fetched_once_per_host():
    fetcher, seen, _ = make_fetcher({PAGE: html(), PAGE + "2": html()})
    fetcher.get(PAGE)
    fetcher.get(PAGE + "2")
    assert sum(r.url.path == "/robots.txt" for r in seen) == 1


def test_robots_redirect_to_html_homepage_on_other_host_is_disallowed():
    """The pabulletin.com trap: robots.txt 302s to a disallowing host's homepage."""
    alias = "https://www.pabulletin.com"
    real = "https://www.pacodeandbulletin.gov"
    routes = {
        alias + "/robots.txt": redirect(real),
        real: html("<html>homepage</html>"),
        real + "/robots.txt": text("User-agent: *\nDisallow: /\n"),
        alias + "/secure/data/index.html": html("content"),
    }
    fetcher, seen, _ = make_fetcher(routes)
    with pytest.raises(RobotsDisallowedError, match="pacodeandbulletin"):
        fetcher.get(alias + "/secure/data/index.html")
    assert alias + "/secure/data/index.html" not in [str(r.url) for r in seen]


def test_robots_redirect_to_html_on_same_host_is_disallowed():
    routes = {
        "https://www.pacourts.us/robots.txt": redirect("/"),
        "https://www.pacourts.us/": html("<html>home</html>"),
    }
    fetcher, _, _ = make_fetcher(routes)
    with pytest.raises(RobotsDisallowedError, match="suspect"):
        fetcher.get(PAGE)


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_robots_error_status_is_disallowed(status):
    fetcher, _, _ = make_fetcher({"https://www.pacourts.us/robots.txt": text("", status=status)})
    with pytest.raises(RobotsDisallowedError):
        fetcher.get(PAGE)


def test_robots_network_error_is_disallowed():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    fetcher, _, _ = make_fetcher({"https://www.pacourts.us/robots.txt": boom})
    with pytest.raises(RobotsDisallowedError, match="unreachable"):
        fetcher.get(PAGE)


def test_content_redirect_to_disallowed_host_is_refused():
    other = "https://elsewhere.example"
    routes = {
        PAGE: redirect(other + "/page"),
        other + "/robots.txt": text("User-agent: *\nDisallow: /\n"),
        other + "/page": html("should not be fetched"),
    }
    fetcher, seen, _ = make_fetcher(routes)
    with pytest.raises(RobotsDisallowedError):
        fetcher.get(PAGE)
    assert other + "/page" not in [str(r.url) for r in seen]


def test_content_redirect_within_host_is_followed():
    fetcher, _, _ = make_fetcher(
        {PAGE: redirect("/courts/new"), PAGE.replace("page", "new"): html("moved")}
    )
    result = fetcher.get(PAGE)
    assert result.url.endswith("/courts/new")
    assert result.content == b"moved"


def test_rate_limit_spaces_requests_to_same_host():
    fetcher, _, clock = make_fetcher({PAGE: html(), PAGE + "2": html()})
    fetcher.get(PAGE)  # robots + page
    fetcher.get(PAGE + "2")
    assert clock.slept == [5, 5]


def test_rate_limit_does_not_sleep_when_enough_time_passed():
    fetcher, _, clock = make_fetcher({PAGE: html()})
    fetcher.robots_for(PAGE)
    clock.now += 60
    fetcher.get(PAGE)
    assert clock.slept == []


def test_request_cap_counts_robots_and_stops():
    config = CONFIG.model_copy(
        update={"limits": CONFIG.limits.model_copy(update={"max_requests_per_run": 2})}
    )
    fetcher, _, _ = make_fetcher({PAGE: html(), PAGE + "2": html()}, config)
    fetcher.get(PAGE)
    assert fetcher.requests_left == 0
    with pytest.raises(RequestCapError):
        fetcher.get(PAGE + "2")


def test_conditional_get_sends_validators_and_reports_not_modified():
    def page(request):
        assert request.headers["if-none-match"] == '"v1"'
        assert request.headers["if-modified-since"] == "Mon, 02 Nov 2026 00:00:00 GMT"
        return httpx.Response(304)

    fetcher, _, _ = make_fetcher({PAGE: page})
    result = fetcher.get(
        PAGE, PageValidators(etag='"v1"', last_modified="Mon, 02 Nov 2026 00:00:00 GMT")
    )
    assert result.not_modified


def test_response_validators_are_returned():
    fetcher, _, _ = make_fetcher({PAGE: html(etag='"abc"', **{"last-modified": "Sun"})})
    assert fetcher.get(PAGE).validators == PageValidators(etag='"abc"', last_modified="Sun")


def test_http_error_raises():
    fetcher, _, _ = make_fetcher({PAGE: html(status=500)})
    with pytest.raises(FetchError, match="500"):
        fetcher.get(PAGE)


def test_refuses_to_run_without_contact_email():
    config = CONFIG.model_copy(update={"contact_email": "CHANGE-ME@example.com"})
    with pytest.raises(ValueError, match="contact email"):
        Fetcher(config)


def test_robots_rules_allow_other_paths():
    robots = "User-agent: *\nDisallow: /private/\n"
    fetcher, _, _ = make_fetcher(
        {"https://www.pacourts.us/robots.txt": text(robots), PAGE: html("ok")}
    )
    assert fetcher.get(PAGE).content == b"ok"


def test_robots_rules_for_our_agent_are_honoured():
    robots = "User-agent: spiderCrawler\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    fetcher, _, _ = make_fetcher({"https://www.pacourts.us/robots.txt": text(robots), PAGE: html()})
    with pytest.raises(RobotsDisallowedError):
        fetcher.get(PAGE)


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr("spidercrawler.fetcher.MAX_BYTES", 10)
    fetcher, _, _ = make_fetcher({PAGE: html("x" * 11)})
    with pytest.raises(FetchError, match="larger than"):
        fetcher.get(PAGE)
