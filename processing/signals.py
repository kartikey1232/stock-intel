"""Rule-based price signals from adjusted daily bars (Phase 5).

Definitions and parameters live in config/signals.yaml; this module only evaluates them.
Signals are rebuilt from full history for each watchlist stock on every run and stored in
the `signals` table (symbol, date, signal, direction, value).

No look-ahead. A signal on day t uses only bars up to and including t's close:
- every rolling window, previous-bar comparison and cooldown looks backwards only, and
  the volume average excludes the signal day itself;
- bars after the latest completed NSE session (e.g. today's before 16:00 IST, which Yahoo
  still revises) are dropped before evaluation;
- prices are corporate-action adjusted, and an adjustment for an action after t rescales
  every bar up to t by the same factor. All rules are ratios or comparisons and every
  stored value is scale-free, so a later action never changes a past signal.
tests/test_signals.py checks that appending future bars (and a future split) leaves every
earlier signal unchanged.

Run with:  uv run python -m processing.signals [--report]
"""

import argparse
import datetime as dt
import logging
import sys

import numpy as np
import pandas as pd
import pandas_ta as ta

from config.loader import Stock, load_watchlist
from config.market_calendar import latest_completed_session, load_holidays
from config.signals import SignalRule, load_signals
from processing.adjustments import adjust_prices
from storage.db import init_db, read_corporate_actions, read_prices, read_signals, replace_signals
from utils import setup_logging

logger = logging.getLogger(__name__)

SIGNAL_COLUMNS = ["date", "signal", "direction", "value"]


# --- rules -------------------------------------------------------------------------


def _crossed(series: pd.Series, level: float, cross: str) -> pd.Series:
    """True where `series` moves from one side of `level` (previous bar) to the other."""
    prev = series.shift(1)
    if cross == "above":
        return (prev <= level) & (series > level)
    return (prev >= level) & (series < level)


def _ma_cross(df: pd.DataFrame, p: dict) -> tuple[pd.Series, pd.Series]:
    fast = df["close"].rolling(p["fast"], min_periods=p["fast"]).mean()
    slow = df["close"].rolling(p["slow"], min_periods=p["slow"]).mean()
    spread = fast / slow - 1
    return _crossed(spread, 0.0, p["cross"]), spread * 100


def _rsi_cross(df: pd.DataFrame, p: dict) -> tuple[pd.Series, pd.Series]:
    length = int(p["length"])
    rsi = ta.rsi(df["close"], length=length, talib=False)
    if rsi is None:  # too few bars
        rsi = pd.Series(np.nan, index=df.index)
    rsi = rsi.copy()
    rsi.iloc[:length] = np.nan  # pandas-ta seeds RSI from bar 2; skip the warm-up
    return _crossed(rsi, float(p["level"]), p["cross"]), rsi


def _volume_spike(df: pd.DataFrame, p: dict) -> tuple[pd.Series, pd.Series]:
    window = int(p["window"])
    volume = df["volume"].astype(float)
    average = volume.rolling(window, min_periods=window).mean().shift(1)  # excludes today
    ratio = volume / average.where(average > 0)
    return ratio > p["multiple"], ratio


def _range_breakout(df: pd.DataFrame, p: dict) -> tuple[pd.Series, pd.Series]:
    window = int(p["window"])
    if p["side"] == "high":
        prior = df["high"].rolling(window, min_periods=window).max().shift(1)
        fired = df["close"] > prior
    else:
        prior = df["low"].rolling(window, min_periods=window).min().shift(1)
        fired = df["close"] < prior
    return fired, (df["close"] / prior - 1) * 100


def _gap(df: pd.DataFrame, p: dict) -> tuple[pd.Series, pd.Series]:
    gap = df["open"] / df["close"].shift(1) - 1
    threshold = p["threshold_pct"] / 100
    fired = gap > threshold if p["side"] == "up" else gap < -threshold
    return fired, gap * 100


RULES = {
    "ma_cross": _ma_cross,
    "rsi_cross": _rsi_cross,
    "volume_spike": _volume_spike,
    "range_breakout": _range_breakout,
    "gap": _gap,
}


def apply_cooldown(fired: pd.Series, cooldown: int) -> pd.Series:
    """Drop firings within `cooldown` bars after a kept firing (looks backwards only)."""
    if cooldown <= 0:
        return fired
    kept = pd.Series(False, index=fired.index)
    last = None
    for position, hit in enumerate(fired.to_numpy()):
        if hit and (last is None or position - last > cooldown):
            kept.iloc[position] = True
            last = position
    return kept


def price_direction(df: pd.DataFrame) -> pd.Series:
    """bullish/bearish/neutral from each bar's close-to-close move."""
    move = df["close"] - df["close"].shift(1)
    return pd.Series(
        np.select([move > 0, move < 0], ["bullish", "bearish"], default="neutral"),
        index=df.index,
    )


def compute_signals(prices: pd.DataFrame, rules: list[SignalRule]) -> pd.DataFrame:
    """Every configured signal for one symbol's (adjusted) bars.

    `prices` needs date, open, high, low, close and volume. Returns SIGNAL_COLUMNS, one row
    per (date, signal) that fired, sorted by date then signal.
    """
    df = prices.sort_values("date").reset_index(drop=True)
    frames = []
    for rule in rules:
        fired, value = RULES[rule.type](df, rule.params)
        fired = apply_cooldown(fired.fillna(False).astype(bool) & value.notna(), rule.cooldown)
        if not fired.any():
            continue
        direction = (
            price_direction(df)[fired]
            if rule.direction == "from_price"
            else pd.Series(rule.direction, index=df.index[fired])
        )
        frames.append(
            pd.DataFrame(
                {
                    "date": df.loc[fired, "date"].to_numpy(),
                    "signal": rule.name,
                    "direction": direction.to_numpy(),
                    "value": value[fired].astype(float).to_numpy(),
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["date", "signal"]).reset_index(drop=True)


# --- display and alerts ------------------------------------------------------------


def describe_signal_value(kind: str, value: float) -> str:
    """Human-readable signal value (see config/signals.yaml for the units)."""
    if kind == "rsi_cross":
        return f"RSI {value:.1f}"
    if kind == "volume_spike":
        return f"{value:.1f}x average volume"
    if kind == "ma_cross":
        return f"SMA spread {value:+.2f}%"
    if kind == "range_breakout":
        return f"{value:+.2f}% beyond the prior 52-week extreme"
    return f"gap {value:+.2f}%"


def confirmed_crosses(prices: pd.DataFrame, rule: SignalRule) -> pd.DataFrame:
    """ma_cross events dated when the averages first stand `min_gap_pct` apart in the
    cross's direction, without having crossed back first (SIGNAL_COLUMNS).

    Known at that day's close, so it's safe for alerts. With min_gap_pct 0 it equals the
    raw cross.
    """
    df = prices.sort_values("date").reset_index(drop=True)
    p = rule.params
    fast = df["close"].rolling(p["fast"], min_periods=p["fast"]).mean()
    slow = df["close"].rolling(p["slow"], min_periods=p["slow"]).mean()
    spread = (fast / slow - 1).to_numpy()
    side = 1 if p["cross"] == "above" else -1
    crossed = _crossed(pd.Series(spread), 0.0, p["cross"]).to_numpy()
    gap = p.get("min_gap_pct", 0.0) / 100
    rows, armed = [], False
    for i, value in enumerate(spread):
        if np.isnan(value):
            continue
        if crossed[i]:
            armed = True
        elif side * value <= 0:
            armed = False  # crossed back before confirming
        if armed and side * value >= gap:
            rows.append((df.at[i, "date"], rule.name, rule.direction, value * 100))
            armed = False
    return pd.DataFrame(rows, columns=SIGNAL_COLUMNS)


def display_signals(
    signals: pd.DataFrame, prices: pd.DataFrame, rules: list[SignalRule]
) -> pd.DataFrame:
    """Signals for the dashboard (and future alerts): stored signals, except that ma_cross
    rules with min_gap_pct > 0 show their confirmed crosses instead of the raw ones.
    `prices` is the symbol's full adjusted history. The stored signals are unchanged."""
    filtered = [r for r in rules if r.type == "ma_cross" and r.params.get("min_gap_pct", 0) > 0]
    if not filtered or prices.empty:
        return signals
    keep = signals[~signals["signal"].isin([r.name for r in filtered])]
    confirmed = [confirmed_crosses(prices, r) for r in filtered]
    out = pd.concat([keep, *confirmed], ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values(["date", "signal"]).reset_index(drop=True)


# --- orchestration -----------------------------------------------------------------


def completed_bars(prices: pd.DataFrame, last_session: dt.date) -> pd.DataFrame:
    """Bars up to the latest completed session (drops a still-changing intraday bar)."""
    return prices[pd.to_datetime(prices["date"]).dt.date <= last_session]


def process_symbol(
    symbol: str, rules: list[SignalRule], last_session: dt.date, now: dt.datetime
) -> int:
    """Rebuild one symbol's stored signals from its adjusted prices. Returns signals stored."""
    raw = read_prices(symbol)
    if raw.empty:
        logger.warning("%s: no prices stored; run collectors.prices first", symbol)
        replace_signals(symbol, [])
        return 0
    prices = completed_bars(adjust_prices(raw, read_corporate_actions(symbol)), last_session)
    signals = compute_signals(prices, rules)
    rows = [
        {
            "symbol": symbol,
            "date": pd.Timestamp(r.date).date(),
            "signal": r.signal,
            "direction": r.direction,
            "value": float(r.value),
            "computed_at": now,
        }
        for r in signals.itertuples(index=False)
    ]
    replace_signals(symbol, rows)
    logger.info("%s: %d signal(s) from %d bar(s) up to %s", symbol, len(rows), len(prices),
                last_session)  # fmt: skip
    return len(rows)


def run(stocks: list[Stock], now: dt.datetime | None = None) -> dict[str, str]:
    """Rebuild signals for every stock; one failure never stops the others.

    Returns failed symbol -> reason.
    """
    now = now or dt.datetime.now(dt.UTC)
    rules = load_signals()
    last_session = latest_completed_session(now, load_holidays())
    failures: dict[str, str] = {}
    for stock in stocks:
        try:
            process_symbol(stock.symbol, rules, last_session, now)
        except Exception as exc:
            logger.exception("%s: signal computation failed", stock.symbol)
            failures[stock.symbol] = f"{type(exc).__name__}: {exc}"
    return failures


def counts_table(signals: pd.DataFrame, rules: list[SignalRule]) -> pd.DataFrame:
    """Signals per signal (rows, in config order) and symbol (columns), with totals."""
    names = [r.name for r in rules]
    if signals.empty:
        return pd.DataFrame(index=names)
    table = pd.crosstab(signals["signal"], signals["symbol"]).reindex(names, fill_value=0)
    table["total"] = table.sum(axis=1)
    return table


def main(argv: list[str] | None = None) -> int:
    """Entry point: rebuild signals for the watchlist; --report prints counts."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="print counts per signal/stock")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    failures = run(load_watchlist())
    if args.report:
        stored = read_signals()
        print(counts_table(stored, load_signals()).to_string())
        if not stored.empty:
            print(f"\n{stored['date'].min()} to {stored['date'].max()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
