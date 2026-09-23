"""Polite HTTP helpers shared by collectors: per-domain rate limiting and retried GETs."""

import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from utils.retry import retry


class RetryableHTTPError(RuntimeError):
    """HTTP 429 or 5xx: worth retrying."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code} from {response.request.url}")
        self.response = response


def domain_of(url: str) -> str:
    """Lowercased host of `url`."""
    return (urlsplit(url).hostname or "").lower()


class DomainRateLimiter:
    """Enforce a minimum interval between requests to the same domain."""

    def __init__(
        self,
        min_interval: Callable[[str], float],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._overrides: dict[str, float] = {}
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def set_min_interval(self, domain: str, seconds: float) -> None:
        """Raise the interval for one domain (e.g. to honour a robots.txt Crawl-delay)."""
        self._overrides[domain] = max(seconds, self._min_interval(domain))

    def wait(self, url: str) -> None:
        """Block until a request to `url`'s domain is allowed, then record it."""
        domain = domain_of(url)
        last = self._last.get(domain)
        if last is not None:
            interval = self._overrides.get(domain, self._min_interval(domain))
            delay = interval - (self._clock() - last)
            if delay > 0:
                self._sleep(delay)
        self._last[domain] = self._clock()


@retry(attempts=3, base_delay=2.0, exceptions=(httpx.TransportError, RetryableHTTPError))
def fetch(client: httpx.Client, url: str, limiter: DomainRateLimiter) -> httpx.Response:
    """GET `url` politely. Retries network errors, 429 and 5xx; other 4xx raise at once.

    Raises:
        httpx.HTTPStatusError: for non-retryable 4xx responses.
        RetryableHTTPError / httpx.TransportError: once retries are exhausted.
    """
    limiter.wait(url)
    response = client.get(url)
    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableHTTPError(response)
    response.raise_for_status()
    return response
