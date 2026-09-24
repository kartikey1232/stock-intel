"""Out-of-sample test of the hypotheses registered in docs/hypotheses.md (H1, H2).

Everything is fixed by the registration: the universe (config/universes.yaml), the
period, the frozen gap rules (config/signals.yaml) and the event-study settings
(config/backtest.yaml). This module only evaluates them, using processing/backtest.py's
own functions, and computes nothing but gap_up and gap_down on the universe.

Run with:  uv run python -m processing.hypotheses
"""

import datetime as dt
import logging
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config.backtest import BacktestConfig, load_backtest_config
from config.signals import SignalRule, load_signals
from config.universes import Universe, load_universe
from processing.backtest import TOO_FEW, bootstrap_ci, build_events, excess_panel, summarise
from processing.signals import compute_signals
from storage.db import init_db, read_prices
from utils import setup_logging

logger = logging.getLogger(__name__)

UNIVERSE = "nifty50_ex_watchlist"
PERIOD = (dt.date(2021, 9, 24), dt.date(2026, 9, 24))
UNRECORDED_ACTION_PCT = 25.0  # |overnight gap| beyond this is an unrecorded corporate action


@dataclass(frozen=True)
class Hypothesis:
    """A registered claim: `signal` at `horizons`, edge on `side` of zero ("above"/"below")."""

    key: str
    text: str
    signal: str
    horizons: tuple[int, ...]
    side: str


HYPOTHESES = (
    Hypothesis(
        "H1",
        "after gap_down (>3%), excess return vs Nifty is negative at 5 and 20 "
        "trading days (continuation)",
        "gap_down",
        (5, 20),
        "above",
    ),
    Hypothesis(
        "H2",
        "after gap_up (>3%), excess return vs Nifty is negative at 1 day",
        "gap_up",
        (1,),
        "below",
    ),
)
LIMITS = """\
Limits: current constituents only (a Wikipedia list dated 2025-12-08; stocks that left
the index or were delisted are missing: survivorship bias); gap days are often market-wide,
so events overlap across stocks and in time, and the bootstrap CIs are too narrow; no
transaction costs, taxes or slippage."""


def frozen_rules(hypotheses: tuple[Hypothesis, ...] = HYPOTHESES) -> list[SignalRule]:
    """The registered signals, exactly as configured (no other signal is computed)."""
    wanted = {h.signal for h in hypotheses}
    return [r for r in load_signals() if r.name in wanted]


def in_period(bars: pd.DataFrame, period: tuple[dt.date, dt.date] = PERIOD) -> pd.DataFrame:
    """Bars within the registered period."""
    dates = pd.to_datetime(bars["date"]).dt.date
    return bars[(dates >= period[0]) & (dates <= period[1])].reset_index(drop=True)


def universe_signals(
    universe: Universe, prices: dict[str, pd.DataFrame], rules: list[SignalRule]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(kept, excluded) gap events for the universe. Excluded: known demerger ex-dates and
    overnight moves beyond UNRECORDED_ACTION_PCT, with a `reason`."""
    frames = []
    for symbol, bars in prices.items():
        found = compute_signals(bars, rules)
        if not found.empty:
            frames.append(found.assign(symbol=symbol))
    if not frames:
        empty = pd.DataFrame(columns=["symbol", "date", "signal", "direction", "value"])
        return empty, empty.assign(reason=[])
    events = pd.concat(frames, ignore_index=True)
    events["date"] = pd.to_datetime(events["date"])
    demerger = [
        (s, d.date()) in universe.known_demergers
        for s, d in zip(events["symbol"], events["date"], strict=True)
    ]
    too_big = events["value"].abs() > UNRECORDED_ACTION_PCT
    reason = np.where(
        demerger,
        "known demerger ex-date",
        np.where(too_big, f"overnight move beyond {UNRECORDED_ACTION_PCT:.0f}%", ""),
    )
    excluded = events[reason != ""].assign(reason=reason[reason != ""])
    return events[reason == ""].reset_index(drop=True), excluded.reset_index(drop=True)


def evaluate(results: pd.DataFrame, events: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """One row per hypothesis x horizon: n, edge and CI, mean excess and CI, verdict."""
    rows = []
    for hyp in HYPOTHESES:
        for h in hyp.horizons:
            r = results[(results["signal"] == hyp.signal) & (results["horizon"] == h)].iloc[0]
            own = events[(events["signal"] == hyp.signal) & events[f"excess_{h}"].notna()]
            ex_low, ex_high = bootstrap_ci(
                own[f"excess_{h}"].to_numpy(), config.bootstrap_samples, config.seed
            )
            if r["note"] == TOO_FEW:
                held = False
            elif hyp.side == "above":
                held = r["ci_low"] > 0
            else:
                held = r["ci_high"] < 0
            rows.append(
                {
                    "hypothesis": hyp.key,
                    "signal": hyp.signal,
                    "horizon": h,
                    "n": r["n"],
                    "edge": r.get("edge"),
                    "ci_low": r.get("ci_low"),
                    "ci_high": r.get("ci_high"),
                    "mean_excess": r.get("mean_excess"),
                    "excess_ci_low": ex_low,
                    "excess_ci_high": ex_high,
                    "held_at_horizon": held,
                }
            )
    return pd.DataFrame(rows)


def verdicts(table: pd.DataFrame) -> dict[str, bool]:
    """A hypothesis holds only if it held at every registered horizon."""
    return {k: bool(g["held_at_horizon"].all()) for k, g in table.groupby("hypothesis")}


def run() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load the registered universe and data, run the test. Returns (table, events,
    excluded events)."""
    config = load_backtest_config()
    universe = load_universe(UNIVERSE)
    rules = frozen_rules()
    benchmark = in_period(read_prices(config.benchmark))
    prices = {}
    for member in universe.members:
        bars = in_period(read_prices(member.symbol))
        if bars.empty:
            logger.warning(
                "%s: no prices stored; run collectors.prices --universe %s", member.symbol, UNIVERSE
            )
            continue
        prices[member.symbol] = bars
    signals, excluded = universe_signals(universe, prices, rules)
    panels = {s: excess_panel(b, benchmark, config.horizons) for s, b in prices.items()}
    events = build_events(signals, panels, config)
    results = summarise(events, config, [r.name for r in rules])
    return evaluate(results, events, config), events, excluded


def pct(value: float) -> str:
    """A fraction as a signed percentage."""
    return f"{value * 100:+.2f}%"


def format_report(
    table: pd.DataFrame, events: pd.DataFrame, excluded: pd.DataFrame, stocks: int
) -> str:
    """The test's results, verdicts, exclusions and limits as text."""
    lines = [
        f"Out-of-sample test, universe {UNIVERSE} ({stocks} stocks with prices), "
        f"{PERIOD[0]} to {PERIOD[1]}",
        "",
    ]
    for r in table.itertuples():
        lines.append(
            f"{r.hypothesis} {r.signal} h={r.horizon:>2}: n={r.n:>4}  edge {pct(r.edge)} "
            f"[{pct(r.ci_low)}, {pct(r.ci_high)}]  mean excess {pct(r.mean_excess)} "
            f"[{pct(r.excess_ci_low)}, {pct(r.excess_ci_high)}]  "
            f"{'held' if r.held_at_horizon else 'did not hold'}"
        )
    lines.append("")
    for key, held in verdicts(table).items():
        text = next(h.text for h in HYPOTHESES if h.key == key)
        lines.append(f"{key} ({text}): {'HELD' if held else 'NOT SUPPORTED'}")
    lines += [
        "",
        "Decision rule (registered): n >= 30 and the edge's 95% CI entirely on the "
        "predicted side of zero at every registered horizon.",
    ]
    if len(excluded):
        listed = [
            f"{r.symbol} {r.date.date()} {r.signal} ({r.reason})" for r in excluded.itertuples()
        ]
        lines.append("Excluded events: " + "; ".join(listed))
    lines += ["", LIMITS]
    return "\n".join(lines)


def main() -> int:
    """Entry point: run the registered out-of-sample test and print the report."""
    setup_logging()
    init_db()
    table, events, excluded = run()
    print(format_report(table, events, excluded, events["symbol"].nunique() if len(events) else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
