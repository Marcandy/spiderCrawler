"""Polite HTTP fetching (PLAN.md section 3, fetching rules).

- robots.txt is checked before any request to a host, following redirects and applying the
  rules of the host it lands on. An HTML, unreachable, or error robots.txt means disallowed.
  (A plain `urllib.robotparser` read of pabulletin.com says "allowed", because it parses the
  homepage HTML it was redirected to. This module must not repeat that.)
- Redirects on content requests are followed by hand so every hop is robots-checked.
- At least `min_delay_seconds` between requests to the same host; at most
  `max_requests_per_run` requests in total, robots.txt requests included.
- Conditional GET with ETag / Last-Modified.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from spidercrawler.config import Config
from spidercrawler.records import PageValidators

MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30.0
MAX_BYTES = 25 * 1024 * 1024  # committee PDFs are well under 1 MB


class FetchError(Exception):
    pass


class RobotsDisallowedError(FetchError):
    pass


class RequestCapError(FetchError):
    pass


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content: bytes
    content_type: str
    validators: PageValidators

    @property
    def not_modified(self) -> bool:
        return self.status == 304


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class _Raw:
    """A fully read, size-capped response."""

    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _is_redirect(resp: _Raw) -> bool:
    # httpx's Response.is_redirect also counts 304, which is not a redirect here.
    return resp.status_code in REDIRECT_STATUSES and "location" in resp.headers


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class _Robots:
    """Robots decision for one origin: parsed rules, allow-all, or deny-all."""

    def __init__(self, parser: RobotFileParser | None, allow_all: bool, reason: str) -> None:
        self._parser = parser
        self._allow_all = allow_all
        self.reason = reason

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self._parser is not None:
            return self._parser.can_fetch(user_agent, url)
        return self._allow_all

    def via(self, origin: str) -> "_Robots":
        return _Robots(self._parser, self._allow_all, f"via {origin}: {self.reason}")


class Fetcher:
    def __init__(
        self,
        config: Config,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not config.has_contact_email:
            raise ValueError("set a real contact email (SPIDERCRAWLER_CONTACT_EMAIL) first")
        self.user_agent = config.user_agent
        self.min_delay = config.limits.min_delay_seconds
        self.max_requests = config.limits.max_requests_per_run
        self.client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, _Robots] = {}
        self.requests_made = 0

    @property
    def requests_left(self) -> int:
        return self.max_requests - self.requests_made

    # -- low level ---------------------------------------------------------------

    def _send(self, url: str, headers: dict[str, str] | None = None) -> _Raw:
        if self.requests_made >= self.max_requests:
            raise RequestCapError(f"request cap of {self.max_requests} reached")
        origin = _origin(url)
        if (last := self._last_request.get(origin)) is not None:
            wait = self.min_delay - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self.requests_made += 1
        request = self.client.build_request(
            "GET", url, headers={"User-Agent": self.user_agent, **(headers or {})}
        )
        try:
            resp = self.client.send(request, stream=True, follow_redirects=False)
            try:
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FetchError(f"{url}: response larger than {MAX_BYTES} bytes")
            finally:
                resp.close()
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {exc}") from exc
        finally:
            self._last_request[origin] = self._clock()
        return _Raw(str(resp.url), resp.status_code, resp.headers, bytes(body))

    # -- robots ------------------------------------------------------------------

    def robots_for(self, url: str) -> _Robots:
        origin = _origin(url)
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin, depth=0)
        return self._robots[origin]

    def _load_robots(self, origin: str, depth: int) -> _Robots:
        robots_url = origin + "/robots.txt"
        try:
            resp = self._send(robots_url)
            hops = 0
            while _is_redirect(resp) and hops < MAX_REDIRECTS:
                resp = self._send(urljoin(resp.url, resp.headers["location"]))
                hops += 1
        except RequestCapError:
            raise
        except (FetchError, KeyError) as exc:
            return _Robots(None, False, f"robots.txt unreachable: {exc}")

        final_origin = _origin(resp.url)
        if _is_redirect(resp):
            return _Robots(None, False, "robots.txt redirect loop")
        if final_origin != origin:
            # Redirected to another host: that host's own robots.txt decides.
            if depth > 0:
                return _Robots(None, False, "robots.txt redirects across hosts twice")
            other = self._robots.get(final_origin) or self._load_robots(final_origin, depth + 1)
            self._robots.setdefault(final_origin, other)
            return other.via(final_origin)
        if resp.status_code in (404, 410):
            return _Robots(None, True, f"no robots.txt ({resp.status_code})")
        content_type = resp.headers.get("content-type", "")
        if resp.status_code == 200 and content_type.startswith("text/plain"):
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return _Robots(parser, False, "robots.txt rules")
        return _Robots(
            None, False, f"suspect robots.txt (status {resp.status_code}, {content_type!r})"
        )

    def check_allowed(self, url: str) -> None:
        robots = self.robots_for(url)
        if not robots.can_fetch(self.user_agent, url):
            raise RobotsDisallowedError(f"{url} disallowed ({robots.reason})")

    # -- content -----------------------------------------------------------------

    def get(self, url: str, validators: PageValidators | None = None) -> FetchResult:
        headers: dict[str, str] = {}
        if validators and validators.etag:
            headers["If-None-Match"] = validators.etag
        if validators and validators.last_modified:
            headers["If-Modified-Since"] = validators.last_modified

        for _ in range(MAX_REDIRECTS + 1):
            self.check_allowed(url)
            resp = self._send(url, headers)
            if not _is_redirect(resp):
                break
            url = urljoin(url, resp.headers.get("location", ""))
        else:
            raise FetchError(f"too many redirects: {url}")

        if resp.status_code != 304 and not 200 <= resp.status_code < 300:
            raise FetchError(f"{url}: HTTP {resp.status_code}")
        return FetchResult(
            url=url,
            status=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type", ""),
            validators=PageValidators(
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
            ),
        )
