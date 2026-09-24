"""Collect daily OHLCV prices from Yahoo Finance for every watchlist stock and benchmark.

First run for a symbol fetches HISTORY_YEARS of history; later runs fetch from the last
stored date onward (re-fetching that date, since it may have been a partial intraday bar).
Rows are upserted, so re-running is always safe.

Failures are explicit, never "no new data":
- A Yahoo rate limit (HTTP 429) is retried once after a long pause; if it persists, the
  remaining symbols are skipped (not requested) and all count as failed.
- An empty response is a failure, because every request covers at least one date that
  has a bar (the last stored date, or five years of history).
- `missing_session_bars` lists stocks without a bar for the latest completed session.

A price-only research universe (config/universes.yaml) is collected the same way with
--universe NAME; it is never part of the daily run_update.py.

Run with:  uv run python -m collectors.prices [--universe NAME]
"""

import argparse
import datetime as dt
import logging
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from config.loader import PriceSeries, load_benchmarks, load_watchlist
from config.market_calendar import latest_completed_session, load_holidays
from storage.db import count_prices, init_db, latest_price_date, upsert_prices
from utils import setup_logging
from utils.retry import retry

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
HISTORY_YEARS = 5
REQUEST_TIMEOUT_S = 20
PAUSE_BETWEEN_TICKERS_S = (1.0, 2.0)
RATE_LIMIT_PAUSE_S = 90.0  # wait this long before the one retry after a Yahoo 429

COLUMN_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}


class NoDataError(RuntimeError):
    """Raised when Yahoo returns no rows where rows were expected."""


@dataclass
class RunSummary:
    """Outcome of one collection run."""

    rows_written: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


def today_ist() -> dt.date:
    """Return today's date on the Indian exchange calendar."""
    return dt.datetime.now(IST).date()


def fetch_start_date(last_stored: dt.date | None, today: dt.date) -> dt.date:
    """Return the first date to request: full history if nothing stored, else the last date."""
    if last_stored is None:
        return today - dt.timedelta(days=365 * HISTORY_YEARS)
    return last_stored


@retry(
    attempts=2,
    base_delay=RATE_LIMIT_PAUSE_S,
    max_delay=RATE_LIMIT_PAUSE_S,
    exceptions=(YFRateLimitError,),
)
@retry(attempts=3, base_delay=2.0, give_up_on=(YFRateLimitError,))
def download_ohlcv(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Download raw daily bars for [start, end] from Yahoo (retried on failure).

    Uses Ticker.history rather than yf.download: yf.download catches every per-ticker
    error, rate limits included, and returns an empty frame. With `hide_exceptions`
    off, history raises YFRateLimitError, YFPricesMissingError, network errors, etc.
    """
    previous = yf.config.debug.hide_exceptions
    yf.config.debug.hide_exceptions = False
    try:
        return yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + dt.timedelta(days=1)).isoformat(),  # yfinance's end is exclusive
            interval="1d",
            auto_adjust=False,
            actions=False,
            timeout=REQUEST_TIMEOUT_S,
        )
    finally:
        yf.config.debug.hide_exceptions = previous


def normalise_ohlcv(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Convert a raw yfinance frame into rows matching the prices table.

    Handles MultiIndex columns (yfinance adds a ticker level even for one ticker),
    timezone-aware indexes (converted to the IST trading date), rows with no price
    data (dropped), and duplicate dates (last one kept).
    """
    if raw.empty:
        return pd.DataFrame(columns=["symbol", "date", *COLUMN_MAP.values()])

    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]

    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if "Adj Close" in missing:  # absent if Yahoo ever applies auto-adjustment
        df["Adj Close"] = df.get("Close")
        missing.remove("Adj Close")
    if missing:
        raise ValueError(f"{symbol}: Yahoo response missing column(s): {', '.join(missing)}")
    df = df[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)

    index = pd.DatetimeIndex(df.index)
    if index.tz is not None:
        index = index.tz_convert(IST).tz_localize(None)
    df.index = index.normalize()

    before = len(df)
    df = df.dropna(subset=["open", "high", "low", "close"], how="all")
    df = df[~_is_holiday_placeholder(df)]
    if dropped := before - len(df):
        logger.info("%s: dropped %d row(s) with no real trading data", symbol, dropped)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    df["volume"] = df["volume"].fillna(0).astype("int64")
    df.index.name = "date"
    df = df.reset_index()
    df.insert(0, "symbol", symbol)
    return df


def _is_holiday_placeholder(df: pd.DataFrame) -> pd.Series:
    """Flag Yahoo's filler bars for exchange holidays: zero volume and a flat OHLC."""
    flat = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
    return flat & (df["volume"].fillna(0) == 0)


def collect_symbol(stock: PriceSeries, today: dt.date) -> int:
    """Fetch, normalise and upsert prices for one stock. Returns rows written."""
    last_stored = latest_price_date(stock.symbol)
    start = fetch_start_date(last_stored, today)
    mode = "full history" if last_stored is None else "incremental"
    logger.info("%s: fetching %s from %s (%s)", stock.symbol, stock.yf, start, mode)

    df = normalise_ohlcv(download_ohlcv(stock.yf, start, today), stock.symbol)
    if df.empty:
        if last_stored is None:
            raise NoDataError(f"no data returned for {stock.yf}; check the ticker")
        # The range starts at a date we already have a bar for, so empty means Yahoo
        # failed (throttling, outage), not "nothing new".
        raise NoDataError(f"no bars returned for {stock.yf} from {start}, which has a bar")

    written = upsert_prices(df)
    new_rows = int((df["date"].dt.date > last_stored).sum()) if last_stored else written
    logger.info(
        "%s: upserted %d row(s) (%d new), %s to %s",
        stock.symbol,
        written,
        new_rows,
        df["date"].min().date(),
        df["date"].max().date(),
    )
    return written


def collect_all(stocks: Sequence[PriceSeries]) -> RunSummary:
    """Collect prices for every stock; one failure never stops the others.

    The exception is a persistent Yahoo rate limit: the remaining stocks are then marked
    failed without being requested, because more requests only prolong the block.
    """
    summary = RunSummary()
    today = today_ist()
    for i, stock in enumerate(stocks):
        if i:
            time.sleep(random.uniform(*PAUSE_BETWEEN_TICKERS_S))
        try:
            summary.rows_written[stock.symbol] = collect_symbol(stock, today)
        except YFRateLimitError:
            logger.error("%s: rate limited by Yahoo; skipping the remaining stocks", stock.symbol)
            summary.failures[stock.symbol] = "rate limited by Yahoo"
            for skipped in stocks[i + 1 :]:
                summary.failures[skipped.symbol] = "skipped: Yahoo rate limit"
            break
        except Exception as exc:
            logger.exception("%s: collection failed", stock.symbol)
            summary.failures[stock.symbol] = f"{type(exc).__name__}: {exc}"
    return summary


def missing_session_bars(
    stocks: Sequence[PriceSeries], now: dt.datetime | None = None
) -> dict[str, str]:
    """Stocks without a stored bar for the latest completed trading session.

    Returns {symbol: reason}. Before 15:30 IST the session checked is the previous
    trading day, so this can run at any time.
    """
    now = now or dt.datetime.now(IST)
    session = latest_completed_session(now, load_holidays())
    missing = {}
    for stock in stocks:
        last = latest_price_date(stock.symbol)
        if last is None or last < session:
            missing[stock.symbol] = f"no bar for {session} (latest stored: {last or 'none'})"
    if missing:
        logger.error(
            "%d stock(s) missing the %s bar: %s", len(missing), session, ", ".join(missing)
        )
    else:
        logger.info("All %d stocks have a bar for %s", len(stocks), session)
    return missing


def log_summary(summary: RunSummary) -> None:
    """Log rows written this run, stored totals per symbol, and any failures."""
    totals = count_prices()
    logger.info("%-10s %8s %10s", "symbol", "this run", "stored")
    for symbol in sorted(set(summary.rows_written) | set(summary.failures)):
        written = summary.rows_written.get(symbol, "FAILED")
        logger.info("%-10s %8s %10d", symbol, written, totals.get(symbol, 0))
    if summary.failures:
        logger.error("%d symbol(s) failed:", len(summary.failures))
        for symbol, reason in summary.failures.items():
            logger.error("  %s: %s", symbol, reason)
    else:
        logger.info("All %d symbols collected successfully", len(summary.rows_written))


def main(argv: list[str] | None = None) -> int:
    """Entry point: collect prices for the watchlist and benchmarks, or with --universe for
    a research universe. Exit code 1 on any failure or missing bar."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--universe", help="collect a universe from config/universes.yaml")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    if args.universe:
        from config.universes import load_universe

        stocks = list(load_universe(args.universe).members)
    else:
        stocks = [*load_watchlist(), *load_benchmarks()]
    summary = collect_all(stocks)
    log_summary(summary)
    missing = missing_session_bars(stocks)
    return 1 if summary.failures or missing else 0


if __name__ == "__main__":
    sys.exit(main())
