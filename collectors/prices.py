"""Collect daily OHLCV prices from Yahoo Finance for every watchlist stock.

First run for a symbol fetches HISTORY_YEARS of history; later runs fetch from the last
stored date onward (re-fetching that date, since it may have been a partial intraday bar).
Rows are upserted, so re-running is always safe.

Run with:  uv run python -m collectors.prices
"""

import datetime as dt
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from config.loader import Stock, load_watchlist
from storage.db import count_prices, init_db, latest_price_date, upsert_prices
from utils import setup_logging
from utils.retry import retry

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
HISTORY_YEARS = 5
REQUEST_TIMEOUT_S = 20
PAUSE_BETWEEN_TICKERS_S = (1.0, 2.0)
STALE_AFTER_DAYS = 7  # warn if an incremental fetch returns nothing and data is this old

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


@retry(attempts=3, base_delay=2.0)
def download_ohlcv(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Download raw daily bars for [start, end] from Yahoo (retried on failure)."""
    return yf.download(
        ticker,
        start=start.isoformat(),
        end=(end + dt.timedelta(days=1)).isoformat(),  # yfinance's end is exclusive
        interval="1d",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        timeout=REQUEST_TIMEOUT_S,
    )


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


def collect_symbol(stock: Stock, today: dt.date) -> int:
    """Fetch, normalise and upsert prices for one stock. Returns rows written."""
    last_stored = latest_price_date(stock.symbol)
    start = fetch_start_date(last_stored, today)
    mode = "full history" if last_stored is None else "incremental"
    logger.info("%s: fetching %s from %s (%s)", stock.symbol, stock.yf, start, mode)

    df = normalise_ohlcv(download_ohlcv(stock.yf, start, today), stock.symbol)
    if df.empty:
        if last_stored is None:
            raise NoDataError(f"no data returned for {stock.yf}; check the ticker")
        if (today - last_stored).days > STALE_AFTER_DAYS:
            logger.warning(
                "%s: no new rows and last stored date is %s; ticker may be delisted or renamed",
                stock.symbol,
                last_stored,
            )
        else:
            logger.info("%s: no new rows (market closed or already up to date)", stock.symbol)
        return 0

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


def collect_all(stocks: list[Stock]) -> RunSummary:
    """Collect prices for every stock; one failure never stops the others."""
    summary = RunSummary()
    today = today_ist()
    for i, stock in enumerate(stocks):
        if i:
            time.sleep(random.uniform(*PAUSE_BETWEEN_TICKERS_S))
        try:
            summary.rows_written[stock.symbol] = collect_symbol(stock, today)
        except Exception as exc:
            logger.exception("%s: collection failed", stock.symbol)
            summary.failures[stock.symbol] = f"{type(exc).__name__}: {exc}"
    return summary


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


def main() -> int:
    """Entry point: collect prices for the whole watchlist. Exit code 1 if any failed."""
    setup_logging()
    init_db()
    summary = collect_all(load_watchlist())
    log_summary(summary)
    return 1 if summary.failures else 0


if __name__ == "__main__":
    sys.exit(main())
