"""Fetch article pages and extract their main text with trafilatura.

Works through articles with text_status = 'pending'. Each run makes at most one attempt
per article (network errors are still retried within that attempt); `text_attempts`
counts attempts across runs so an article is never retried forever:

    ok         main text extracted (at least min_chars)
    paywalled  schema.org isAccessibleForFree=false or HTTP 402; final, text left empty
    skipped    unresolvable Google News link, or robots.txt disallows the URL; final
    failed     404/410/non-HTML (final), or max_attempts transient failures
               (403/blocked, 5xx, timeouts, too little text) used up
    pending    a transient failure with attempts left: retried on the next run

Title and summary are never touched, so paywalled and failed articles keep them.
Site furniture trafilatura keeps (promo footers, "Top Trending Stocks" lists) is removed
line by line using the per-domain `boilerplate` regexes in config/news_sources.yaml;
--reclean re-applies the current rules to already-stored text without re-fetching.

Run with:  uv run python -m collectors.article_text [--limit N] [--reclean]
"""

import argparse
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from config.news_sources import NewsConfig, load_news_sources
from storage.db import (
    init_db,
    pending_text_articles,
    read_extracted_texts,
    set_article_texts,
    text_status_counts,
    update_article_text,
)
from utils import setup_logging
from utils.http import DomainRateLimiter, RetryableHTTPError, domain_of, fetch

logger = logging.getLogger(__name__)

GOOGLE_NEWS_HOST = "news.google.com"
PAYWALL_RE = re.compile(r'"isaccessibleforfree"\s*:\s*"?false"?', re.IGNORECASE)
BLOCKED_CODES = {401, 403, 451}
GONE_CODES = {404, 410}


@dataclass(frozen=True)
class Outcome:
    """Result of one extraction attempt, before attempt counting."""

    status: str  # ok | paywalled | skipped | failed | retry
    text: str | None = None
    error: str | None = None
    counts_as_attempt: bool = True


class RobotsUnavailable(RuntimeError):
    """robots.txt could not be fetched (network error or 5xx); try again next run."""


class RobotsCache:
    """Fetches and caches robots.txt per origin, honouring Crawl-delay."""

    def __init__(self, client: httpx.Client, limiter: DomainRateLimiter, user_agent: str):
        self._client = client
        self._limiter = limiter
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        """True if robots.txt permits our User-Agent to fetch `url`.

        Raises:
            RobotsUnavailable: if robots.txt can't be fetched right now.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            self._parsers[origin] = self._load(origin)
        return self._parsers[origin].can_fetch(self._user_agent, url)

    def _load(self, origin: str) -> RobotFileParser:
        """Fetch robots.txt: 401/403 disallow everything, other 4xx allow everything."""
        parser = RobotFileParser()
        try:
            response = fetch(self._client, f"{origin}/robots.txt", self._limiter)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                parser.disallow_all = True
            else:
                parser.allow_all = True
            return parser
        except (RetryableHTTPError, httpx.TransportError) as exc:
            raise RobotsUnavailable(f"robots.txt unavailable for {origin}: {exc}") from exc

        parser.parse(response.text.splitlines())
        delay = parser.crawl_delay(self._user_agent)
        if delay:
            self._limiter.set_min_interval(domain_of(origin), float(delay))
        return parser


def boilerplate_patterns(config: NewsConfig, url: str) -> list[re.Pattern[str]]:
    """Compiled boilerplate line patterns for `url`'s domain plus the '*' patterns."""
    raw = config.text_boilerplate.get("*", []) + config.text_boilerplate.get(domain_of(url), [])
    return [re.compile(p) for p in raw]


def strip_boilerplate(text: str, patterns: list[re.Pattern[str]]) -> str:
    """Drop whole lines that match any pattern at their start."""
    kept = [line for line in text.splitlines() if not any(p.match(line.strip()) for p in patterns)]
    return "\n".join(kept).strip()


def extract_outcome(
    response: httpx.Response, min_chars: int, boilerplate: list[re.Pattern[str]] | None = None
) -> Outcome:
    """Classify a successfully fetched page: ok, paywalled, or a (retryable) failure."""
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        return Outcome("failed", error=f"not HTML ({content_type or 'no content-type'})")
    html = response.text
    if PAYWALL_RE.search(html):
        return Outcome("paywalled", error="isAccessibleForFree=false")
    text = trafilatura.extract(html, url=str(response.url), favor_precision=True)
    if text and boilerplate:
        text = strip_boilerplate(text, boilerplate)
    if not text or len(text) < min_chars:
        return Outcome("retry", error=f"extracted only {len(text or '')} chars")
    return Outcome("ok", text=text)


def attempt(
    article: dict[str, Any],
    client: httpx.Client,
    limiter: DomainRateLimiter,
    robots: RobotsCache,
    config: NewsConfig,
) -> Outcome:
    """Make one extraction attempt for an article. Never raises."""
    url = article["url"]
    if domain_of(url) == GOOGLE_NEWS_HOST:
        return Outcome("skipped", error="unresolved Google News link", counts_as_attempt=False)
    try:
        if not robots.allowed(url):
            return Outcome("skipped", error="disallowed by robots.txt", counts_as_attempt=False)
        response = fetch(client, url, limiter)
    except RobotsUnavailable as exc:
        return Outcome("retry", error=str(exc))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 402:
            return Outcome("paywalled", error="HTTP 402")
        if code in GONE_CODES:
            return Outcome("failed", error=f"HTTP {code}")
        reason = " (blocked)" if code in BLOCKED_CODES else ""
        return Outcome("retry", error=f"HTTP {code}{reason}")
    except (RetryableHTTPError, httpx.TransportError) as exc:
        return Outcome("retry", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # unexpected: record it rather than crash the run
        logger.exception("Unexpected error fetching %s", url)
        return Outcome("retry", error=f"{type(exc).__name__}: {exc}")
    return extract_outcome(
        response, config.text_min_chars, boilerplate_patterns(config, str(response.url))
    )


def apply_outcome(article: dict[str, Any], outcome: Outcome, max_attempts: int) -> str:
    """Persist an outcome, turning 'retry' into pending/failed by attempt count."""
    attempts = article["text_attempts"] + (1 if outcome.counts_as_attempt else 0)
    status = outcome.status
    if status == "retry":
        status = "failed" if attempts >= max_attempts else "pending"
    update_article_text(article["id"], status, attempts, text=outcome.text, error=outcome.error)
    return status


def interleave_by_domain(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin articles across domains so per-domain rate-limit waits overlap."""
    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        by_domain[domain_of(article["url"])].append(article)
    rounds = zip_longest(*by_domain.values())
    return [a for batch in rounds for a in batch if a is not None]


def extract_pending(
    config: NewsConfig,
    limit: int | None = None,
    client: httpx.Client | None = None,
    limiter: DomainRateLimiter | None = None,
) -> Counter[tuple[str, str]]:
    """Attempt extraction for pending articles. Returns counts of (source, new status)."""
    articles = interleave_by_domain(pending_text_articles(config.text_max_attempts, limit))
    logger.info("%d article(s) pending text extraction", len(articles))
    limiter = limiter or DomainRateLimiter(config.min_interval)
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": config.user_agent},
        timeout=config.timeout_s,
        follow_redirects=True,
    )
    robots = RobotsCache(client, limiter, config.user_agent)
    results: Counter[tuple[str, str]] = Counter()
    try:
        for article in articles:
            outcome = attempt(article, client, limiter, robots, config)
            status = apply_outcome(article, outcome, config.text_max_attempts)
            results[(article["source"] or "(unknown)", status)] += 1
            if status != "ok" and outcome.status != "skipped":
                logger.info("%s -> %s: %s", article["url"], status, outcome.error)
    finally:
        if own_client:
            client.close()
    return results


def reclean_stored(config: NewsConfig) -> int:
    """Re-apply the current boilerplate rules to stored text. Returns texts changed.

    A text left shorter than min_chars was mostly boilerplate, so it goes back to
    'pending' and is retried (within the usual attempt cap) like a fresh short extraction.
    """
    changed = {}
    for row in read_extracted_texts():
        cleaned = strip_boilerplate(row["text"], boilerplate_patterns(config, row["url"]))
        if cleaned == row["text"]:
            continue
        if len(cleaned) < config.text_min_chars:
            update_article_text(
                row["id"],
                "pending",
                row["text_attempts"],
                error=f"only {len(cleaned)} chars left after boilerplate removal",
            )
        else:
            changed[row["id"]] = cleaned
    set_article_texts(changed)
    logger.info("Re-cleaned %d stored text(s)", len(changed))
    return len(changed)


def log_summary(results: Counter[tuple[str, str]]) -> None:
    """Log this run's outcomes and the stored status totals for the main sources."""
    run = Counter(status for (_, status) in results.elements())
    logger.info("This run: %s", dict(run) or "nothing to do")
    totals = text_status_counts()
    if totals.empty:
        return
    totals["total"] = totals.sum(axis=1)
    top = totals.sort_values("total", ascending=False).head(15)
    for line in top.to_string().splitlines():
        logger.info("%s", line)


def main(argv: list[str] | None = None) -> int:
    """Entry point: extract text for pending articles."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None, help="max articles this run")
    parser.add_argument(
        "--reclean", action="store_true", help="re-apply boilerplate rules to stored text"
    )
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    config = load_news_sources()
    if args.reclean:
        reclean_stored(config)
        return 0
    log_summary(extract_pending(config, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
