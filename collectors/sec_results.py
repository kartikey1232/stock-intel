"""Fetch the Indian quarterly results INFY and HDFCBANK furnish to the SEC with Form 6-K.

Both are NYSE-listed and file each quarter's Indian results (Ind AS for Infosys, Indian
GAAP for HDFC Bank, in ₹ crore or lakh) with the SEC. That's the same content they file
with NSE/BSE, from a host we may access (EDGAR is public, and the SEC's fair-access policy
allows automated requests that declare a contact email and stay under 10 per second).
Source ranking in processing/results.py: XBRL > SEC 6-K > IR PDF.

Exhibits are chosen by content, not exhibit number (HDFC Bank's is usually EX-99,
Infosys's EX-99.3, and in 2021 HDFC Bank put its results in the 6-K body itself): every
HTML document in a 6-K is parsed, and one counts only if it names the company, states
Ind AS / Indian GAAP, and has a ₹ crore/lakh standalone or consolidated results table for
a quarter (Infosys's IFRS US-dollar summary is skipped). A document that has a results
table but fails those checks, with no other document in the 6-K passing them, fails the
6-K loudly rather than being guessed at.

Validation: before an exhibit is stored, every figure it prints for a quarter and basis
we also have as XBRL must equal the XBRL value rounded to the exhibit's printed
precision. Any other difference fails the 6-K with symbol, quarter, basis and metric, and
nothing from it is stored.

Politeness: User-Agent "stock-intel <SEC_CONTACT_EMAIL>" (from .env), at most one request
per second per host, timeouts, retries with backoff, robots.txt checked. 6-Ks already
examined are recorded in `sec_filings_checked` and never fetched again; failed ones aren't
recorded, so they're retried (and fail again) every run until resolved. A 6-K filed in
the results window of a quarter we already have from the SEC on both bases is skipped
unfetched, and an exhibit whose quarter and bases are already stored is not stored again
(outcome "duplicate": Infosys files its results in two 6-Ks each quarter). After a
parser fix, `--recheck` forgets 6-Ks recorded as having no results, so they're examined
again.

Run with:  uv run python -m collectors.sec_results [--recheck]
"""

import argparse
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass

import httpx
import pandas as pd
from dotenv import load_dotenv

from collectors.article_text import RobotsCache
from collectors.result_files import store_result_file
from config.loader import Stock, load_watchlist
from processing.results import (
    ParsedResult,
    ResultParseError,
    fiscal_quarter,
    has_results_table,
    parse_file,
    parse_sec_exhibit,
    rebuild,
    sec_mismatches,
)
from storage.db import (
    checked_sec_filings,
    forget_sec_filings_without_results,
    init_db,
    mark_sec_filing_checked,
    project_path,
    read_result_files,
)
from utils import setup_logging
from utils.http import DomainRateLimiter, fetch

logger = logging.getLogger(__name__)

SEC_SOURCES = {"INFY": "0001067491", "HDFCBANK": "0001144967"}  # symbol -> EDGAR CIK
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"
FORMS = {"6-K", "6-K/A"}
MIN_INTERVAL_S = 1.0  # per host; SEC allows 10 requests/s
TIMEOUT_S = 30.0
BACKFILL_YEARS = 5
BASES = ("standalone", "consolidated")
RESULTS_WINDOW_DAYS = 75  # a quarter's results 6-K is filed within this many days of its end


class SecConfigError(RuntimeError):
    """SEC_CONTACT_EMAIL is missing from .env."""


class SecValidationError(RuntimeError):
    """A 6-K's figures disagree with XBRL; the message lists every mismatch."""


@dataclass(frozen=True)
class SixK:
    """One 6-K from a company's EDGAR submissions list."""

    accession: str  # 0001193125-26-308398
    filed_on: dt.date

    @property
    def folder(self) -> str:
        """The accession without dashes, as used in archive URLs."""
        return self.accession.replace("-", "")


def user_agent() -> str:
    """The declared User-Agent SEC requires: a name and a contact email.

    Raises:
        SecConfigError: if SEC_CONTACT_EMAIL isn't set.
    """
    load_dotenv()
    email = os.getenv("SEC_CONTACT_EMAIL", "").strip()
    if not email:
        raise SecConfigError("SEC_CONTACT_EMAIL is not set in .env; SEC requires a contact")
    return f"stock-intel {email}"


def six_ks(submissions: dict, since: dt.date) -> list[SixK]:
    """6-Ks (and amendments) filed on or after `since`, oldest first."""
    recent = submissions["filings"]["recent"]
    found = [
        SixK(accession, dt.date.fromisoformat(filed))
        for form, accession, filed in zip(
            recent["form"], recent["accessionNumber"], recent["filingDate"], strict=True
        )
        if form in FORMS and dt.date.fromisoformat(filed) >= since
    ]
    return sorted(found, key=lambda f: (f.filed_on, f.accession))


def window_quarter(filed_on: dt.date) -> dt.date | None:
    """The quarter end whose results window (end, end + RESULTS_WINDOW_DAYS] has `filed_on`."""
    end = (pd.Timestamp(filed_on) - pd.Timedelta(days=1) + pd.offsets.QuarterEnd(0)).date()
    if end >= filed_on:
        end = (pd.Timestamp(end) - pd.offsets.QuarterEnd(1)).date()
    return end if (filed_on - end).days <= RESULTS_WINDOW_DAYS else None


def html_documents(index: dict) -> list[str]:
    """Names of a filing's HTML documents, excluding EDGAR's generated index pages."""
    names = [item["name"] for item in index["directory"]["item"]]
    return [n for n in names if n.lower().endswith((".htm", ".html")) and "-index" not in n]


def select_exhibits(
    documents: list[tuple[str, bytes]], stock: Stock, stocks: list[Stock]
) -> list[tuple[str, bytes, list[ParsedResult]]]:
    """The documents that are this company's Indian quarterly results, parsed.

    Raises:
        ResultParseError: if a document has a results table but none passes the content
            checks (see module docstring); the message gives each document's reason.
    """
    selected, rejected = [], []
    for name, content in documents:
        try:
            parsed = parse_sec_exhibit(content, stocks)
            if parsed[0].company != stock.symbol:
                raise ResultParseError(f"results are for {parsed[0].company}")
            selected.append((name, content, parsed))
        except ResultParseError as exc:
            if has_results_table(content):
                rejected.append(f"{name}: {exc}")
    if not selected and rejected:
        raise ResultParseError("results tables found but no exhibit matched: "
                               + "; ".join(rejected))  # fmt: skip
    for reason in rejected:
        logger.info("%s: not the Indian results: %s", stock.symbol, reason)
    return selected


def stored_xbrl(symbol: str) -> dict[tuple[dt.date, str], dict[str, float]]:
    """(period_end, basis) -> metric -> value, from every XBRL file stored for `symbol`.

    Read from the files rather than the results table, so XBRL imported earlier in the
    same run counts.
    """
    files = read_result_files()
    out: dict[tuple[dt.date, str], dict[str, float]] = {}
    for f in files[files["symbol"] == symbol].itertuples(index=False):
        path = project_path(f.attachment_path)
        if path.suffix != ".xml":
            continue
        try:
            parsed = parse_file(path, [])
        except (ResultParseError, OSError) as exc:
            logger.error("%s: can't read stored XBRL %s: %s", symbol, f.attachment_path, exc)
            continue
        for p in parsed:
            out[(p.period_end, p.basis)] = {m: v for m, (v, _) in p.values.items()}
    return out


def check_against_xbrl(
    symbol: str, parsed: list[ParsedResult], xbrl: dict[tuple[dt.date, str], dict[str, float]]
) -> int:
    """Compare each exhibit section with XBRL for its quarter and basis, where we have it.

    Returns the number of sections compared.

    Raises:
        SecValidationError: listing every mismatch.
    """
    problems, compared = [], 0
    for p in parsed:
        if (p.period_end, p.basis) in xbrl:
            compared += 1
            problems += sec_mismatches(symbol, p, xbrl[(p.period_end, p.basis)])
    if problems:
        raise SecValidationError("6-K figures differ from XBRL: " + "; ".join(problems))
    return compared


def collect_symbol(
    stock: Stock,
    cik: str,
    client: httpx.Client,
    limiter: DomainRateLimiter,
    robots: RobotsCache,
    stocks: list[Stock],
    today: dt.date,
) -> tuple[dict[str, int], list[str]]:
    """Check new 6-Ks for one company. Returns (counts by outcome, failure messages)."""
    counts = {"stored": 0, "duplicate": 0, "no_results": 0, "skipped": 0, "validated": 0}
    failures: list[str] = []
    submissions_url = SUBMISSIONS_URL.format(cik=cik)
    if not robots.allowed(submissions_url):
        return counts, [f"robots.txt disallows {submissions_url}"]
    submissions = fetch(client, submissions_url, limiter).json()
    checked = checked_sec_filings(stock.symbol)
    bases_found: dict[dt.date, set[str]] = {}
    for end, bases in checked.values():
        if end:
            bases_found.setdefault(end, set()).update(bases)
    xbrl = stored_xbrl(stock.symbol)
    since = today.replace(year=today.year - BACKFILL_YEARS)
    for filing in six_ks(submissions, since):
        if filing.accession in checked:
            continue
        if bases_found.get(window_quarter(filing.filed_on), set()) >= set(BASES):
            counts["skipped"] += 1
            continue
        base = ARCHIVE_URL.format(cik=int(cik), folder=filing.folder)
        try:
            documents = fetch_documents(base, client, limiter, robots)
            exhibits = select_exhibits(documents, stock, stocks)
            for _, _, parsed in exhibits:
                counts["validated"] += check_against_xbrl(stock.symbol, parsed, xbrl)
        except (ResultParseError, SecValidationError) as exc:
            failures.append(f"{filing.accession} ({filing.filed_on}): {exc}")
            logger.error("%s 6-K %s filed %s: %s", stock.symbol, filing.accession,
                         filing.filed_on, exc)  # fmt: skip
            continue
        period_end, bases = None, set()
        new = [(n, c, ps) for n, c, ps in exhibits
               if any(p.basis not in bases_found.get(p.period_end, set()) for p in ps)]  # fmt: skip
        for name, content, parsed in new:
            for p in parsed:
                p.filed_on = p.filed_on or filing.filed_on  # EDGAR date if no board date
            store_result_file(content, "htm", stock, parsed, "SEC", source_url=base + name,
                              original_name=f"{filing.accession}/{name}")  # fmt: skip
            period_end = parsed[0].period_end
            bases |= {p.basis for p in parsed if p.period_end == period_end}
            bases_found.setdefault(period_end, set()).update(bases)
            label = ", ".join(f"{fiscal_quarter(p.period_end)} {p.basis}" for p in parsed)
            logger.info("%s: stored 6-K %s %s (%s)", stock.symbol, filing.accession, name, label)
        outcome = "results" if new else ("duplicate" if exhibits else "none")
        counts[{"results": "stored", "duplicate": "duplicate", "none": "no_results"}[outcome]] += 1
        mark_sec_filing_checked(
            {
                "accession": filing.accession,
                "symbol": stock.symbol,
                "filed_on": filing.filed_on,
                "period_end": period_end,
                "bases": "+".join(sorted(bases)) or None,
                "outcome": outcome,
                "checked_at": dt.datetime.now(dt.UTC),
            }
        )
    return counts, failures


def fetch_documents(
    base: str, client: httpx.Client, limiter: DomainRateLimiter, robots: RobotsCache
) -> list[tuple[str, bytes]]:
    """(name, content) of every HTML document in one filing folder."""
    index = fetch(client, base + "index.json", limiter).json()
    documents = []
    for name in html_documents(index):
        if robots.allowed(base + name):
            documents.append((name, fetch(client, base + name, limiter).content))
    return documents


def collect_all(stocks: list[Stock], client: httpx.Client | None = None) -> dict[str, str]:
    """Collect every configured company; one failure never stops the others.

    Returns symbol -> failure message(s).
    """
    by_symbol = {s.symbol: s for s in stocks}
    limiter = DomainRateLimiter(lambda _domain: MIN_INTERVAL_S)
    own = client is None
    if client is None:
        client = httpx.Client(headers={"User-Agent": user_agent()}, timeout=TIMEOUT_S,
                              follow_redirects=True)  # fmt: skip
    ua = client.headers.get("User-Agent", "")
    robots = RobotsCache(client, limiter, ua)
    today = dt.datetime.now(dt.UTC).date()
    failures: dict[str, str] = {}
    try:
        for symbol, cik in SEC_SOURCES.items():
            if symbol not in by_symbol:
                continue
            try:
                counts, errors = collect_symbol(by_symbol[symbol], cik, client, limiter, robots,
                                                stocks, today)  # fmt: skip
                logger.info("%s SEC 6-Ks: %s", symbol, counts)
            except Exception as exc:
                logger.exception("%s: SEC collection failed", symbol)
                errors = [f"{type(exc).__name__}: {exc}"]
            if errors:
                failures[symbol] = "; ".join(errors)
    finally:
        if own:
            client.close()
    return failures


def main(argv: list[str] | None = None) -> int:
    """Entry point: fetch new 6-K results, then rebuild the results table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recheck", action="store_true",
                        help="re-examine 6-Ks previously found to have no results")  # fmt: skip
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    stocks = load_watchlist()
    if args.recheck:
        logger.info("Forgot %d 6-K(s) recorded as having no results",
                    forget_sec_filings_without_results())  # fmt: skip
    failures = collect_all(stocks)
    rebuild(stocks)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
