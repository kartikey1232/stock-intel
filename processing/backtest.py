"""Event study: what happened after each rule-based price signal (Phase 5).

For every stored signal (processing/signals.py) and horizon h in config/backtest.yaml:

- Return. A signal is known at the close of day t, so the trade starts at the next open:
  the h-day return runs from open(t+1) to close(t+h) on corporate-action adjusted bars.
  The excess return subtracts the benchmark's (Nifty 50) return over the same window.
- Baseline ("doing nothing"). The same stock's excess return from every day that has a
  full window, measured the same way. edge = direction x (event excess - that stock's
  baseline excess), so a positive edge means the signal did better than a random day in
  the direction it points (a bearish signal "works" when the stock underperforms). The
  baseline includes the event days themselves, which pulls the edge slightly towards
  zero for frequent signals (conservative).
- Clusters. Events of the same signal and stock within `cluster_gap` trading days of the
  previous one form a chain; only the first counts.
- Statistics per (signal, horizon): n, mean and median excess return, hit rate (excess in
  the signal's direction), the baseline hit rate, mean edge and a bootstrap 95% CI for it
  (percentile method, fixed seed). n < min_events is marked "too few events to judge".

Volume spikes take each day's price direction, so their mean excess mixes up and down
days; read their edge and hit rate, which are signed per event. Neutral (flat-day) events
have no direction and are left out.

Run with:  uv run python -m processing.backtest [--csv PATH]
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from config.backtest import BacktestConfig, load_backtest_config
from config.loader import Stock, load_watchlist
from config.signals import load_signals
from processing.adjustments import adjust_prices
from storage.db import init_db, read_corporate_actions, read_prices, read_signals
from utils import setup_logging

logger = logging.getLogger(__name__)

SIGNS = {"bullish": 1, "bearish": -1}
TOO_FEW = "too few events to judge"
LIMITATIONS = """\
Limits of this study:
- Universe: 5 hand-picked large caps that are still listed and still large today
  (survivorship bias); about 5 years of daily data; results pooled across the 5 stocks.
- Many signals x horizons are tested at once ({tests} results, {judged} with enough
  events), so at a 95% level about {chance:.1f} could look significant by chance alone.
  Treat a single good number as a hypothesis, not a finding.
- No transaction costs, taxes, slippage or position sizing; entry at the next open.
- The baseline (all days of the same stock) includes the event days, so edges of
  frequent signals are slightly understated.
- Events on the same dates across stocks and overlapping windows at the 20 and 60-day
  horizons are not independent, so the bootstrap intervals are too narrow.
- Signal parameters have not been tuned; tuning them on this data would overfit it."""


# --- returns -----------------------------------------------------------------------


def forward_windows(bars: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    """For each bar t: entry_date = t+1, and per h the exit_date t+h and the return from
    open(t+1) to close(t+h). NaN where the window runs past the data."""
    df = bars.sort_values("date").reset_index(drop=True)
    out = pd.DataFrame({"date": df["date"], "entry_date": df["date"].shift(-1)})
    entry = df["open"].shift(-1)
    for h in horizons:
        out[f"exit_date_{h}"] = df["date"].shift(-h)
        out[f"ret_{h}"] = df["close"].shift(-h) / entry - 1
    return out


def excess_panel(
    bars: pd.DataFrame, benchmark: pd.DataFrame, horizons: tuple[int, ...]
) -> pd.DataFrame:
    """forward_windows plus the benchmark's return over each identical window, and the
    excess return (stock - benchmark). Windows whose dates the benchmark lacks are NaN."""
    panel = forward_windows(bars, horizons)
    bench = benchmark.set_index("date")
    bench_open = bench["open"].reindex(panel["entry_date"]).to_numpy()
    for h in horizons:
        bench_close = bench["close"].reindex(panel[f"exit_date_{h}"]).to_numpy()
        panel[f"bench_{h}"] = bench_close / bench_open - 1
        panel[f"excess_{h}"] = panel[f"ret_{h}"] - panel[f"bench_{h}"]
    return panel


# --- events ------------------------------------------------------------------------


def first_in_clusters(events: pd.DataFrame, positions: pd.Series, gap: int) -> pd.Series:
    """True for events that start a cluster: no event of the same symbol and signal within
    `gap` bars before them (chained, so a long run of daily firings is one cluster)."""
    keep = pd.Series(False, index=events.index)
    for _, group in events.assign(pos=positions).groupby(["symbol", "signal"]):
        previous = None
        for index, position in group["pos"].sort_values().items():
            keep[index] = previous is None or position - previous > gap
            previous = position
    return keep


def build_events(
    signals: pd.DataFrame, panels: dict[str, pd.DataFrame], config: BacktestConfig
) -> pd.DataFrame:
    """Declustered events joined to their forward/excess returns and stock baselines."""
    rows = []
    for symbol, panel in panels.items():
        own = signals[signals["symbol"] == symbol].copy()
        if own.empty:
            continue
        own["date"] = pd.to_datetime(own["date"])
        position = pd.Series(panel.index, index=panel["date"])
        own = own[own["date"].isin(position.index)]
        own = own[first_in_clusters(own, own["date"].map(position), config.cluster_gap)]
        merged = own.merge(panel, on="date", how="left")
        for h in config.horizons:
            valid = panel[f"excess_{h}"].dropna()
            merged[f"base_{h}"] = valid.mean()
            merged[f"base_up_{h}"] = (valid > 0).mean()
        rows.append(merged)
    if not rows:
        return pd.DataFrame()
    events = pd.concat(rows, ignore_index=True)
    events["sign"] = events["direction"].map(SIGNS).fillna(0).astype(int)
    return events


# --- statistics --------------------------------------------------------------------


def bootstrap_ci(values: np.ndarray, samples: int, seed: int) -> tuple[float, float]:
    """Percentile 95% CI of the mean of `values`."""
    if len(values) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = values[rng.integers(0, len(values), size=(samples, len(values)))].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def summarise(events: pd.DataFrame, config: BacktestConfig, order: list[str]) -> pd.DataFrame:
    """One row per (signal, horizon) with the statistics in the module docstring."""
    rows = []
    for signal in order:
        own = (
            events[(events["signal"] == signal) & (events["sign"] != 0)] if len(events) else events
        )
        for h in config.horizons:
            ev = own[own[f"excess_{h}"].notna()] if len(own) else own
            n = len(ev)
            row = {"signal": signal, "horizon": h, "n": n}
            if n:
                sign = ev["sign"].to_numpy()
                excess = ev[f"excess_{h}"].to_numpy()
                edge = sign * (excess - ev[f"base_{h}"].to_numpy())
                base_hit = np.where(sign > 0, ev[f"base_up_{h}"], 1 - ev[f"base_up_{h}"])
                low, high = bootstrap_ci(edge, config.bootstrap_samples, config.seed)
                row |= {
                    "mean_return": float(ev[f"ret_{h}"].mean()),
                    "mean_excess": float(excess.mean()),
                    "median_excess": float(np.median(excess)),
                    "hit_rate": float((sign * excess > 0).mean()),
                    "baseline_hit_rate": float(base_hit.mean()),
                    "edge": float(edge.mean()),
                    "ci_low": low,
                    "ci_high": high,
                }
            row["note"] = TOO_FEW if n < config.min_events else _verdict(row)
            rows.append(row)
    return pd.DataFrame(rows)


def _verdict(row: dict) -> str:
    """Whether the edge's 95% CI excludes zero (before any multiple-testing correction)."""
    if row["ci_low"] > 0:
        return "beats baseline (CI > 0)"
    if row["ci_high"] < 0:
        return "worse than baseline (CI < 0)"
    return "no clear difference"


# --- orchestration -----------------------------------------------------------------


def load_panels(stocks: list[Stock], config: BacktestConfig) -> dict[str, pd.DataFrame]:
    """Adjusted-price excess-return panels for each stock with stored prices."""
    benchmark = read_prices(config.benchmark)
    if benchmark.empty:
        raise RuntimeError(f"no {config.benchmark} prices stored; run collectors.prices")
    panels = {}
    for stock in stocks:
        raw = read_prices(stock.symbol)
        if raw.empty:
            logger.warning("%s: no prices stored; skipped", stock.symbol)
            continue
        bars = adjust_prices(raw, read_corporate_actions(stock.symbol))
        panels[stock.symbol] = excess_panel(bars, benchmark, config.horizons)
    return panels


def run_event_study(
    stocks: list[Stock], config: BacktestConfig | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(results table, event rows) for every stored signal of `stocks`."""
    config = config or load_backtest_config()
    order = [r.name for r in load_signals()]
    events = build_events(read_signals(), load_panels(stocks, config), config)
    return summarise(events, config, order), events


def format_report(results: pd.DataFrame, events: pd.DataFrame, signals: pd.DataFrame) -> str:
    """The results table as text, with event counts and the study's limits."""
    table = results.copy()
    for col in ("mean_excess", "median_excess", "edge"):
        table[col] = table[col].map(lambda v: f"{v * 100:+.2f}%" if pd.notna(v) else "-")
    for col in ("hit_rate", "baseline_hit_rate"):
        table[col] = table[col].map(lambda v: f"{v * 100:.0f}%" if pd.notna(v) else "-")
    table["95% CI (edge)"] = [
        f"[{lo * 100:+.2f}%, {hi * 100:+.2f}%]" if pd.notna(lo) else "-"
        for lo, hi in zip(results["ci_low"], results["ci_high"], strict=True)
    ]
    table = table.rename(columns={"horizon": "h", "baseline_hit_rate": "base hit"})
    columns = ["signal", "h", "n", "mean_excess", "median_excess", "hit_rate", "base hit",
               "edge", "95% CI (edge)", "note"]  # fmt: skip
    raw = signals.groupby("signal").size()
    kept = events.groupby("signal").size() if len(events) else pd.Series(dtype=int)
    counts = ", ".join(f"{s} {raw.get(s, 0)}->{kept.get(s, 0)}" for s in results["signal"].unique())
    tests, judged = len(results), int((results["note"] != TOO_FEW).sum())
    return "\n".join(
        [
            table[columns].to_string(index=False),
            "",
            f"Events before -> after declustering: {counts}",
            "edge = signal direction x (event excess return - same stock's excess return from "
            "all days); hit rate = excess return in the signal's direction.",
            "",
            LIMITATIONS.format(tests=tests, judged=judged, chance=tests * 0.05),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the event study on stored signals and print the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, help="also write the results table here")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    results, events = run_event_study(load_watchlist())
    print(format_report(results, events, read_signals()))
    if args.csv:
        results.to_csv(args.csv, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
