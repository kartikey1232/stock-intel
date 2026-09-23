"""Import quarterly results files (XBRL or PDF) dropped into data/filings/inbox/.

Files are identified by their content, not their name: company (the NSE symbol in the
XBRL, or the company name on a PDF's first page), quarter end and standalone/consolidated.
Accepted files move to data/filings/<SYMBOL>/<sha256>.<ext> and get a `filings` row; the
results table is then rebuilt. Anything else is moved to inbox/rejected/ with a
<name>.reason.txt saying why; exact duplicates of stored files go to inbox/duplicates/.
Nothing is ever deleted.

`checklist` shows, per stock, which of the last 8 quarters x {standalone, consolidated}
are imported (XBRL), available only as a lower-trust PDF, or missing, with the exact NSE
page to download each missing one from.

Run with:
  uv run python -m collectors.result_files import
  uv run python -m collectors.result_files checklist
"""

import argparse
import datetime as dt
import hashlib
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config.loader import Stock, load_watchlist
from processing.results import (
    ParsedResult,
    ResultParseError,
    filing_date_and_subject,
    fiscal_quarter,
    identify_company,
    parse_file,
    rebuild,
)
from storage.db import PROJECT_ROOT, filing_by_sha256, init_db, insert_filing, read_results
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
FILINGS_DIR = PROJECT_ROOT / "data" / "filings"
INBOX = FILINGS_DIR / "inbox"
BASES = ("standalone", "consolidated")
RESULTS_DUE_DAYS = 60  # results are due within 45 days (60 for Q4) of the quarter end
INTEGRATED_FILING_START = dt.date(2025, 3, 31)  # SEBI integrated filing from Q4 FY25


@dataclass
class ImportOutcome:
    """What happened to one inbox file."""

    name: str
    status: str  # imported | duplicate | rejected
    detail: str


def sha256_of(content: bytes) -> str:
    """Hex SHA-256 of `content`."""
    return hashlib.sha256(content).hexdigest()


def identify(path: Path, stocks: list[Stock]) -> tuple[Stock, list[ParsedResult]]:
    """Work out which stock, quarter(s) and basis a results file is for.

    Raises:
        ResultParseError: with a human-readable reason if it can't be identified.
    """
    head = path.read_bytes()[:512].lstrip()
    if head.startswith(b"%PDF"):
        kind = "pdf"
    elif head.startswith(b"<?xml") or b"<xbrl" in head or b":xbrl" in head:
        kind = "xbrl"
    elif b"<html" in head.lower() or b"<!doctype html" in head.lower():
        raise ResultParseError(
            "this is an HTML page (inline XBRL or a saved web page). On NSE, download the "
            "'XBRL' (.xml) link instead of 'iXBRL'"
        )
    else:
        raise ResultParseError("not a PDF or XBRL file")

    parsed = parse_file(path, stocks)
    if kind == "pdf":
        symbol = parsed[0].company
        stock = next(s for s in stocks if s.symbol == symbol)
    else:
        stock = _stock_for_xbrl(parsed[0], stocks)
    for p in parsed:
        try:
            fiscal_quarter(p.period_end)
        except ValueError as exc:
            raise ResultParseError(f"{exc}; only quarterly results are imported") from exc
    return stock, parsed


def _stock_for_xbrl(p: ParsedResult, stocks: list[Stock]) -> Stock:
    """Match an XBRL file to a watchlist stock by its NSE symbol, else its company name."""
    hint = (p.symbol_hint or "").strip().upper()
    for stock in stocks:
        names = {stock.symbol, *(a.upper() for a in stock.aliases),
                 *(n.upper() for c in stock.conditional_aliases for n in c.names)}  # fmt: skip
        if hint and hint in names:
            return stock
    if p.company and (stock := identify_company(p.company, stocks)):
        return stock
    raise ResultParseError(
        f"company not in the watchlist (symbol {p.symbol_hint!r}, name {p.company!r})"
    )


def store_result_file(
    content: bytes,
    ext: str,
    stock: Stock,
    parsed: list[ParsedResult],
    exchange: str,
    source_url: str | None = None,
    original_name: str | None = None,
) -> str | None:
    """Save a results file under data/filings/<SYMBOL>/ and add its filings row.

    Returns the filing id, or None if a file with the same content is already stored.
    """
    digest = sha256_of(content)
    if filing_by_sha256(digest):
        return None
    folder = FILINGS_DIR / stock.symbol
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.{ext}"
    path.write_bytes(content)

    filed_on, subject = filing_date_and_subject(parsed)
    now = dt.datetime.now(dt.UTC)
    filing_id = f"file-{digest[:40]}"
    insert_filing(
        {
            "id": filing_id,
            "exchange": exchange,
            "exchange_id": digest[:40],
            "symbol": stock.symbol,
            "filed_at": dt.datetime.combine(filed_on, dt.time(), tzinfo=IST).astimezone(dt.UTC),
            "first_seen_at": now,
            "category": "Financial Results",
            "subject": subject,
            "description": original_name,
            "attachment_url": source_url,
            "attachment_path": str(path),
            "attachment_sha256": digest,
            "filing_type": "results",
            "filing_tags": "results",
        }
    )
    return filing_id


def import_inbox(stocks: list[Stock], inbox: Path = INBOX) -> list[ImportOutcome]:
    """Import every file in the inbox (see module docstring)."""
    inbox.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")):
        try:
            stock, parsed = identify(path, stocks)
        except (ResultParseError, OSError) as exc:
            _move(path, inbox / "rejected")
            (inbox / "rejected" / f"{path.name}.reason.txt").write_text(f"{exc}\n", "utf-8")
            outcomes.append(ImportOutcome(path.name, "rejected", str(exc)))
            continue

        content = path.read_bytes()
        ext = "pdf" if content.lstrip().startswith(b"%PDF") else "xml"
        exchange = "NSE" if parsed[0].kind == "xbrl" and parsed[0].symbol_hint else "manual"
        label = ", ".join(f"{fiscal_quarter(p.period_end)} {p.basis}" for p in parsed)
        if store_result_file(content, ext, stock, parsed, exchange, original_name=path.name):
            path.unlink()  # its content now lives under data/filings/<SYMBOL>/
            outcomes.append(ImportOutcome(path.name, "imported", f"{stock.symbol} {label}"))
        else:
            _move(path, inbox / "duplicates")
            outcomes.append(ImportOutcome(path.name, "duplicate", f"{stock.symbol} {label}"))
    return outcomes


def _move(path: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists():
        target = folder / f"{path.stem}-{sha256_of(path.read_bytes())[:8]}{path.suffix}"
    shutil.move(str(path), target)


# --- checklist ---------------------------------------------------------------------


def last_quarters(today: dt.date, count: int = 8) -> list[dt.date]:
    """The last `count` quarter ends whose results are due by `today`, newest first."""
    ends: list[dt.date] = []
    end = pd.Timestamp(today) + pd.offsets.QuarterEnd(0)
    while len(ends) < count:
        if (end.date() + dt.timedelta(days=RESULTS_DUE_DAYS)) <= today:
            ends.append(end.date())
        end -= pd.offsets.QuarterEnd(1)
    return ends


def download_page(symbol: str, period_end: dt.date) -> str:
    """NSE page listing the results file for this quarter."""
    if period_end >= INTEGRATED_FILING_START:
        return (
            "https://www.nseindia.com/companies-listing/corporate-integrated-filing"
            f"?integratedType=integratedfilingfinancials&symbol={symbol}"
        )
    return f"https://www.nseindia.com/companies-listing/corporate-filings-financial-results?symbol={symbol}"


def checklist(stocks: list[Stock], today: dt.date) -> pd.DataFrame:
    """Per stock x quarter x basis: xbrl | pdf (lower trust) | missing, and where to get it."""
    results = read_results()
    have = {
        (r.symbol, pd.Timestamp(r.period_end).date(), r.basis): r.source
        for r in results.drop_duplicates(["symbol", "period_end", "basis"]).itertuples()
    }
    rows = []
    for stock in stocks:
        for end in last_quarters(today):
            for basis in BASES:
                source = have.get((stock.symbol, end, basis))
                status = {"xbrl": "ok (XBRL)", "pdf": "PDF only - XBRL would upgrade"}.get(
                    source, "MISSING"
                )
                rows.append(
                    {
                        "symbol": stock.symbol,
                        "quarter": fiscal_quarter(end),
                        "period_end": end,
                        "basis": basis,
                        "status": status,
                        "download_from": ""
                        if source == "xbrl"
                        else download_page(stock.symbol, end),
                    }
                )
    return pd.DataFrame(rows)


def print_checklist(table: pd.DataFrame) -> None:
    """Print the checklist grouped by stock, with per-stock totals."""
    for symbol, g in table.groupby("symbol", sort=False):
        done = (g["status"] == "ok (XBRL)").sum()
        print(f"\n{symbol}: {done}/{len(g)} imported as XBRL")
        for r in g.itertuples(index=False):
            mark = "✓" if r.status == "ok (XBRL)" else ("~" if r.status.startswith("PDF") else "✗")
            line = f"  {mark} {r.quarter} {r.period_end} {r.basis:<12} {r.status}"
            print(line + (f"\n      -> {r.download_from}" if r.download_from else ""))
    print(
        "\nOn the NSE page, pick the quarter and the Standalone/Consolidated row, download its "
        "XBRL (.xml, not iXBRL), save it into data/filings/inbox/ with any name, then run:\n"
        "  uv run python -m collectors.result_files import"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: `import` the inbox or print the `checklist`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["import", "checklist"])
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    stocks = load_watchlist()
    if args.command == "import":
        outcomes = import_inbox(stocks)
        for o in outcomes:
            log = logger.warning if o.status == "rejected" else logger.info
            log("%-9s %s: %s", o.status, o.name, o.detail)
        if not outcomes:
            logger.info("Inbox %s is empty", INBOX)
        rebuild(stocks)
        return 1 if any(o.status == "rejected" for o in outcomes) else 0
    print_checklist(checklist(stocks, dt.datetime.now(IST).date()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
