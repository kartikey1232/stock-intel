"""Collect raw news articles from Google News (one query per stock) and publisher RSS feeds.

Only fetches and stores raw articles; matching articles to stocks and sentiment belong in
processing/. Articles are keyed by a hash of their normalised URL and are never
overwritten, so re-runs are idempotent and first_seen_at records when we first had them.
Full article text is not fetched here: text_status starts as 'pending'.

Google News links are Google redirect URLs (news.google.com/rss/articles/...). They are
not HTTP redirects and cannot be resolved reliably, so they are stored as-is; the
publisher comes from the feed's <source> element.

Run with:  uv run python -m collectors.news
"""

import calendar
import datetime as dt
import hashlib
import html
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from config.loader import Stock, load_watchlist
from config.news_sources import NewsConfig, load_news_sources
from storage.db import count_articles, init_db, insert_new_articles
from utils import setup_logging
from utils.http import DomainRateLimiter, fetch

logger = logging.getLogger(__name__)

GOOGLE_PREFIX = "google:"
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid",
    "_ga", "ocid", "cmpid", "ref", "ref_src", "from", "oc",
}  # fmt: skip
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


class FeedError(RuntimeError):
    """The response was not a usable RSS/Atom feed."""


@dataclass(frozen=True)
class Source:
    """One feed URL to fetch."""

    name: str  # stored as fetched_via
    url: str
    publisher: str | None  # None for Google News: publisher comes from each entry
    is_google: bool = False


@dataclass
class SourceResult:
    """Outcome of fetching one source."""

    name: str
    found: int = 0
    new: int = 0
    error: str | None = None


# --- URL handling ------------------------------------------------------------------


def normalise_url(url: str) -> str:
    """Canonical form of `url` for de-duplication.

    Forces https, lowercases the host, drops default ports, fragments, trailing slashes,
    utm_* and other tracking parameters, and sorts the remaining query parameters.
    """
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)
    )
    return urlunsplit((scheme, host, parts.path.rstrip("/"), urlencode(query), ""))


def article_id(url: str) -> str:
    """Stable 32-hex-character id for an article, from its normalised URL."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()[:32]


def google_news_url(stock: Stock, config: NewsConfig) -> str:
    """Google News RSS search URL for a stock's search terms."""
    terms = " OR ".join(f'"{term}"' for term in stock.search_terms)
    query = f"{terms} when:{config.google_news_window}"
    params = {"q": query, **config.google_news_params}
    return f"{config.google_news_url}?{urlencode(params)}"


def build_sources(config: NewsConfig, stocks: list[Stock]) -> list[Source]:
    """All sources for a run: publisher feeds, then one Google News query per stock."""
    sources = [Source(f.name, f.url, f.publisher) for f in config.feeds]
    sources += [
        Source(f"{GOOGLE_PREFIX}{s.symbol}", google_news_url(s, config), None, is_google=True)
        for s in stocks
    ]
    return sources


# --- parsing -----------------------------------------------------------------------


def clean_text(value: str | None) -> str | None:
    """Strip HTML tags and entities and collapse whitespace; None if nothing is left."""
    if not value:
        return None
    text = SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", value))).strip()
    return text or None


def parse_published(entry: Any) -> dt.datetime | None:
    """Entry's published (or updated) time as aware UTC, or None if missing/unparseable."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return dt.datetime.fromtimestamp(calendar.timegm(parsed), tz=dt.UTC)
    except (OverflowError, ValueError):
        return None


def parse_feed(content: bytes, source: Source, seen_at: dt.datetime) -> list[dict[str, Any]]:
    """Turn raw feed bytes into article rows for the articles table.

    Raises:
        FeedError: if the content isn't a parseable feed.
    """
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise FeedError(f"not a valid feed: {feed.get('bozo_exception')}")

    rows = []
    for entry in feed.entries:
        link, title = entry.get("link"), clean_text(entry.get("title"))
        if not link or not title:
            continue
        publisher = source.publisher
        summary = clean_text(entry.get("summary"))
        if source.is_google:
            publisher = clean_text((entry.get("source") or {}).get("title"))
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(f" - {publisher}")]
            summary = None  # Google's summary is just the title and publisher as HTML
        rows.append(
            {
                "id": article_id(link),
                "url": normalise_url(link),
                "source": publisher,
                "title": title,
                "summary": summary,
                "published_at": parse_published(entry),
                "first_seen_at": seen_at,
                "fetched_via": source.name,
            }
        )
    return rows


# --- orchestration -----------------------------------------------------------------


def collect_source(
    client: httpx.Client, limiter: DomainRateLimiter, source: Source
) -> SourceResult:
    """Fetch, parse and store one source. Never raises; errors go in the result."""
    result = SourceResult(source.name)
    try:
        content = fetch(client, source.url, limiter).content
        rows = parse_feed(content, source, seen_at=dt.datetime.now(dt.UTC))
        result.found = len(rows)
        result.new = insert_new_articles(rows)
        logger.info("%s: %d entries, %d new", source.name, result.found, result.new)
    except Exception as exc:
        logger.exception("%s: failed to collect %s", source.name, source.url)
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def collect_all(
    config: NewsConfig,
    stocks: list[Stock],
    client: httpx.Client | None = None,
    limiter: DomainRateLimiter | None = None,
) -> list[SourceResult]:
    """Collect every source; one failure never stops the others."""
    limiter = limiter or DomainRateLimiter(config.min_interval)
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": config.user_agent},
        timeout=config.timeout_s,
        follow_redirects=True,
    )
    try:
        return [collect_source(client, limiter, s) for s in build_sources(config, stocks)]
    finally:
        if own_client:
            client.close()


def log_summary(results: list[SourceResult]) -> None:
    """Log per-source results, stored totals and failures."""
    stored = count_articles("fetched_via")
    logger.info("%-22s %6s %5s %7s", "source", "found", "new", "stored")
    for r in results:
        found = "FAILED" if r.error else r.found
        logger.info("%-22s %6s %5d %7d", r.name, found, r.new, stored.get(r.name, 0))
    failures = [r for r in results if r.error]
    for r in failures:
        logger.error("FAILED %s: %s", r.name, r.error)
    total_new = sum(r.new for r in results)
    logger.info(
        "%d new article(s); %d/%d sources OK", total_new, len(results) - len(failures), len(results)
    )


def main() -> int:
    """Entry point: collect news for all sources. Exit code 1 if any source failed."""
    setup_logging()
    init_db()
    results = collect_all(load_news_sources(), load_watchlist())
    log_summary(results)
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
