"""Fetch quarterly results PDFs from company investor-relations sites that allow it.

Only HDFC Bank qualifies: its results page links the PDFs statically and robots.txt
allows them. (Infosys and TCS block automated clients; TMPV loads its list via a private
JS API; RIL publishes no results files we could find; none publish XBRL.) These PDFs are
lower-trust than XBRL, so processing/results.py uses them only where no XBRL is imported.

Each run fetches the results page, then every linked quarterly-results PDF not yet stored
(by URL, then by content hash). A PDF is kept only if it parses as that company's results.
The page lists several years, so the first run is the backfill; it's slow on purpose
(per-domain rate limit) and resumable (stored files are skipped next time).

Run with:  uv run python -m collectors.results_ir
"""

import datetime as dt
import logging
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import httpx

from collectors.article_text import RobotsCache
from collectors.result_files import store_result_file
from config.loader import Stock, load_watchlist
from config.news_sources import load_news_sources
from processing.results import ResultParseError, parse_results_pdf, pdf_pages, rebuild
from storage.db import filing_urls, init_db
from utils import setup_logging
from utils.http import DomainRateLimiter, fetch

logger = logging.getLogger(__name__)

BACKFILL_YEARS = 5  # skip PDFs in fiscal-year folders older than this

IR_SOURCES = {
    "HDFCBANK": {
        "page": "https://www.hdfc.bank.in/about-us/investor-relations/financial-results",
        # "financial-results-for-(the-)quarter-ended-june-30-2026.pdf" and variants such as
        # "...-quarter-and-year-ended-..." / "...-quarter-and-nine-months-ended-..."; not
        # press releases, "key-parameters" summaries or review reports. The static page
        # links only one quarter per fiscal year (others load via JS tabs); those quarters
        # come from the inbox instead.
        "pdf": re.compile(
            r"/financial-results-for-(?:the-)?quarter(?:-and-[a-z-]+?)?-ended-[^/]*\.pdf$"
        ),
    },
}


def result_links(page_html: str, base_url: str, pattern: re.Pattern[str]) -> list[str]:
    """Absolute URLs of results PDFs linked from an IR page, deduplicated and sorted."""
    links = {urljoin(base_url, h) for h in re.findall(r'href="([^"]+\.pdf)"', page_html)}
    return sorted(link for link in links if pattern.search(link))


def recent_enough(url: str, today: dt.date, years: int = BACKFILL_YEARS) -> bool:
    """True if the URL's fiscal-year folder ("/2023-2024/") is within `years` of today.

    URLs without such a folder are kept (the parsed period is checked later anyway).
    """
    m = re.search(r"/(\d{4})-\d{4}/", url)
    return m is None or int(m.group(1)) >= today.year - years


def collect_symbol(
    stock: Stock,
    source: dict,
    client: httpx.Client,
    limiter: DomainRateLimiter,
    robots: RobotsCache,
    stocks: list[Stock],
) -> dict[str, int]:
    """Download new results PDFs for one company. Returns counts by outcome."""
    counts = {"new": 0, "known": 0, "skipped": 0}
    if not robots.allowed(source["page"]):
        logger.warning("%s: robots.txt disallows %s", stock.symbol, source["page"])
        return counts
    page = fetch(client, source["page"], limiter)
    known = filing_urls(stock.symbol)
    today = dt.datetime.now(dt.UTC).date()
    for url in result_links(page.text, str(page.url), source["pdf"]):
        if not recent_enough(url, today):
            continue
        if url in known:
            counts["known"] += 1
            continue
        if not robots.allowed(url):
            counts["skipped"] += 1
            continue
        try:
            content = fetch(client, url, limiter).content
        except httpx.HTTPStatusError as exc:
            logger.warning("%s: skipping %s: HTTP %s (dead link)", stock.symbol,
                           url.rsplit("/", 1)[-1], exc.response.status_code)  # fmt: skip
            counts["skipped"] += 1
            continue
        if not content.lstrip().startswith(b"%PDF"):
            logger.warning("%s: skipping %s: not a PDF (dead link or error page)", stock.symbol,
                           url.rsplit("/", 1)[-1])  # fmt: skip
            counts["skipped"] += 1
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                Path(tmp.name).write_bytes(content)
                parsed = parse_results_pdf(pdf_pages(Path(tmp.name)), stocks)
            if parsed[0].company != stock.symbol:
                raise ResultParseError(f"file is for {parsed[0].company}, not {stock.symbol}")
        except ResultParseError as exc:
            logger.warning("%s: skipping %s: %s", stock.symbol, url.rsplit("/", 1)[-1], exc)
            counts["skipped"] += 1
            continue
        if store_result_file(content, "pdf", stock, parsed, "IR", source_url=url,
                             original_name=url.rsplit("/", 1)[-1]):  # fmt: skip
            counts["new"] += 1
            logger.info("%s: stored %s", stock.symbol, url.rsplit("/", 1)[-1])
        else:
            counts["known"] += 1  # same content already stored under another URL
    return counts


def collect_all(stocks: list[Stock], client: httpx.Client | None = None) -> dict[str, str]:
    """Collect every configured IR source; one failure never stops the others.

    Returns symbol -> error message for failures.
    """
    config = load_news_sources()
    limiter = DomainRateLimiter(config.min_interval)
    own = client is None
    client = client or httpx.Client(
        headers={"User-Agent": config.user_agent}, timeout=config.timeout_s, follow_redirects=True
    )
    robots = RobotsCache(client, limiter, config.user_agent)
    by_symbol = {s.symbol: s for s in stocks}
    failures = {}
    try:
        for symbol, source in IR_SOURCES.items():
            if symbol not in by_symbol:
                continue
            try:
                counts = collect_symbol(by_symbol[symbol], source, client, limiter, robots, stocks)
                logger.info("%s IR results: %s", symbol, counts)
            except Exception as exc:
                logger.exception("%s: IR collection failed", symbol)
                failures[symbol] = f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            client.close()
    return failures


def main() -> int:
    """Entry point: fetch new IR results PDFs, then rebuild the results table."""
    setup_logging()
    init_db()
    stocks = load_watchlist()
    failures = collect_all(stocks)
    rebuild(stocks)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
