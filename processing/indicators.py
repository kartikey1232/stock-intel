"""Compute technical indicators from stored prices and upsert them.

Indicators are computed from corporate-action adjusted OHLC (see processing.adjustments),
so splits and demergers don't show up as crashes. Raw prices are also checked for large
overnight gaps with no recorded corporate action, which are logged as warnings.

Recursive indicators
(EMA, MACD, RSI, ATR) depend on all prior bars, so incremental runs recompute over
LOOKBACK_BARS of history before the last stored indicator date. That is far more than
the 200 bars SMA-200 needs, and long enough for the exponential smoothing to converge
to the full-history values within floating-point noise. Use --full to recompute
everything.

Run with:  uv run python -m processing.indicators [--full]
"""

import argparse
import logging
import sys

import pandas as pd
import pandas_ta as ta

from config.loader import load_benchmarks, load_watchlist
from processing.adjustments import adjust_prices, sync_actions_from_config, warn_unrecorded_gaps
from storage.db import (
    init_db,
    latest_indicator_date,
    read_corporate_actions,
    read_prices,
    upsert_indicators,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

LOOKBACK_BARS = 600
RSI_LENGTH = 14
INDICATOR_COLUMNS = [
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_20",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "atr_14",
    "volume_sma_20",
]


def compute_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """Compute every indicator column from a single symbol's prices.

    `prices` must have date, high, low, close and volume columns. Returns a frame with
    symbol, date and INDICATOR_COLUMNS, one row per input row. Warm-up periods are NaN.
    """
    df = prices.sort_values("date").set_index("date")
    close, high, low = df["close"], df["high"], df["low"]

    rsi = ta.rsi(close, length=RSI_LENGTH, talib=False)
    if rsi is not None:
        # pandas-ta emits RSI from bar 2 (seeded on one price change, often exactly 0 or
        # 100); blank the warm-up so RSI starts at bar RSI_LENGTH + 1 like charting tools.
        rsi.iloc[:RSI_LENGTH] = float("nan")
    macd = ta.macd(close, fast=12, slow=26, signal=9, talib=False)
    bbands = ta.bbands(close, length=20, lower_std=2.0, upper_std=2.0, talib=False)

    out = pd.DataFrame(
        {
            "rsi_14": rsi,
            "macd": _column(macd, "MACD_"),
            "macd_signal": _column(macd, "MACDs_"),
            "macd_hist": _column(macd, "MACDh_"),
            "sma_20": ta.sma(close, length=20, talib=False),
            "sma_50": ta.sma(close, length=50, talib=False),
            "sma_200": ta.sma(close, length=200, talib=False),
            "ema_20": ta.ema(close, length=20, talib=False),
            "bb_upper": _column(bbands, "BBU_"),
            "bb_middle": _column(bbands, "BBM_"),
            "bb_lower": _column(bbands, "BBL_"),
            "atr_14": ta.atr(high, low, close, length=14, talib=False),
            "volume_sma_20": ta.sma(df["volume"].astype(float), length=20, talib=False),
        },
        index=df.index,
    )
    out.insert(0, "symbol", df["symbol"].iloc[0] if "symbol" in df else None)
    return out.reset_index()


def _column(frame: pd.DataFrame | None, prefix: str) -> pd.Series | None:
    """Pick the single pandas-ta output column whose name starts with `prefix`."""
    if frame is None:  # pandas-ta returns None when there are too few rows
        return None
    matches = [c for c in frame.columns if c.startswith(prefix)]
    if len(matches) != 1:
        raise KeyError(f"expected one column starting {prefix!r}, got {list(frame.columns)}")
    return frame[matches[0]]


def process_symbol(symbol: str, full: bool = False) -> int:
    """Compute and upsert indicators for one symbol. Returns rows written."""
    raw = read_prices(symbol)
    if raw.empty:
        logger.warning("%s: no prices stored; run collectors.prices first", symbol)
        return 0
    actions = read_corporate_actions(symbol)
    warn_unrecorded_gaps(symbol, raw, actions)
    prices = adjust_prices(raw, actions)

    since = None if full else latest_indicator_date(symbol)
    if since is not None:
        # Keep LOOKBACK_BARS of history before `since` so every indicator is fully warmed up.
        first_needed = max(0, int((prices["date"].dt.date < since).sum()) - LOOKBACK_BARS)
        prices = prices.iloc[first_needed:]

    result = compute_indicators(prices)
    if since is not None:
        result = result[result["date"].dt.date >= since]

    written = upsert_indicators(result)
    logger.info(
        "%s: upserted %d indicator row(s) (%s, from %s)",
        symbol,
        written,
        "full" if since is None else "incremental",
        result["date"].min().date() if written else "-",
    )
    return written


def process_all(
    symbols: list[str], full: bool = False, force_full: set[str] | None = None
) -> dict[str, str]:
    """Compute indicators for every symbol; one failure never stops the others.

    Symbols in `force_full` (e.g. whose corporate actions just changed) are fully
    recomputed even when `full` is False. Returns failed symbol -> reason.
    """
    force_full = force_full or set()
    failures: dict[str, str] = {}
    for symbol in symbols:
        try:
            process_symbol(symbol, full=full or symbol in force_full)
        except Exception as exc:
            logger.exception("%s: indicator computation failed", symbol)
            failures[symbol] = f"{type(exc).__name__}: {exc}"

    for symbol, reason in failures.items():
        logger.error("FAILED %s: %s", symbol, reason)
    return failures


def main(argv: list[str] | None = None) -> int:
    """Entry point: compute indicators for the watchlist and benchmarks. Exit 1 on failures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="recompute all history")
    args = parser.parse_args(argv)

    setup_logging()
    init_db()
    changed = sync_actions_from_config()
    symbols = [s.symbol for s in [*load_watchlist(), *load_benchmarks()]]
    failures = process_all(symbols, full=args.full, force_full=changed)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
