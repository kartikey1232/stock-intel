"""Tests for the registered out-of-sample test (synthetic prices; no network, no real DB)."""

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import collectors.prices as prices
import processing.hypotheses as hyp
from config.backtest import BacktestConfig
from config.loader import Benchmark
from config.universes import Universe, UniverseError, load_universe
from processing.backtest import build_events, excess_panel, summarise

CONFIG = BacktestConfig(
    horizons=(1, 5, 20, 60),
    benchmark="NIFTY50",
    cluster_gap=10,
    min_events=30,
    bootstrap_samples=2000,
    seed=1,
)


def test_only_the_registered_signals_are_computed() -> None:
    rules = hyp.frozen_rules()
    assert sorted(r.name for r in rules) == ["gap_down", "gap_up"]
    assert all(r.params["threshold_pct"] == 3.0 for r in rules)  # frozen at registration


def test_project_universe_excludes_the_watchlist() -> None:
    universe = load_universe(hyp.UNIVERSE)
    symbols = {m.symbol for m in universe.members}
    assert len(symbols) == 45
    assert not symbols & {"RELIANCE", "TCS", "HDFCBANK", "INFY", "TMPV", "NIFTY50"}
    assert ("ITC", dt.date(2025, 1, 6)) in universe.known_demergers


def test_universe_members_may_not_duplicate_tracked_symbols(tmp_path: Path) -> None:
    path = tmp_path / "u.yaml"
    path.write_text(
        'universes:\n  u:\n    members:\n      - {symbol: INFY, yf: "INFY.NS", name: Infosys}\n'
    )
    with pytest.raises(UniverseError, match="already tracked"):
        load_universe("u", path)


def gap_bars(n: int, gap_days: list[int], gap: float, after: float, seed: int) -> pd.DataFrame:
    """A stock tracking a quiet market, with open gaps of `gap` on `gap_days` and an extra
    move `after` over the following days."""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.004, n)
    for day in gap_days:
        ret[day + 2 : day + 7] += after / 5
    close = 100 * np.cumprod(1 + ret)
    opens = np.roll(close, 1)
    opens[0] = close[0]
    for day in gap_days:
        opens[day] = close[day - 1] * (1 + gap)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2022-01-03", periods=n),
            "open": opens,
            "high": np.maximum(opens, close),
            "low": np.minimum(opens, close),
            "close": close,
            "volume": 1000,
        }
    )


def run_synthetic(after: float, demergers=frozenset()) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = 900
    bench = gap_bars(n, [], 0, 0, seed=99)
    stocks = {
        f"S{i}": gap_bars(n, list(range(40 + i, 860, 60)), -0.05, after, seed=i) for i in range(4)
    }
    universe = Universe("u", tuple(Benchmark(s, f"{s}.NS", s) for s in stocks), demergers)
    signals, excluded = hyp.universe_signals(universe, stocks, hyp.frozen_rules())
    panels = {s: excess_panel(b, bench, CONFIG.horizons) for s, b in stocks.items()}
    events = build_events(signals, panels, CONFIG)
    results = summarise(events, CONFIG, ["gap_up", "gap_down"])
    return hyp.evaluate(results, events, CONFIG), excluded


def test_continuation_after_gap_down_makes_h1_hold() -> None:
    table, _ = run_synthetic(after=-0.04)
    h1 = table[table["hypothesis"] == "H1"].set_index("horizon")
    assert (h1["n"] >= 30).all() and (h1["ci_low"] > 0).all()
    assert hyp.verdicts(table)["H1"] is True
    assert hyp.verdicts(table)["H2"] is False  # no gap-ups at all: n = 0


def test_no_continuation_means_h1_is_not_supported() -> None:
    table, _ = run_synthetic(after=0.0)
    assert hyp.verdicts(table)["H1"] is False


def test_demerger_dates_and_huge_gaps_are_excluded() -> None:
    first_s0_gap = pd.bdate_range("2022-01-03", periods=900)[40].date()
    _, excluded = run_synthetic(after=0.0, demergers=frozenset({("S0", first_s0_gap)}))
    assert excluded["reason"].tolist() == ["known demerger ex-date"]
    universe = Universe("u", (), frozenset())
    bars = gap_bars(100, [50], -0.40, 0, seed=5)
    kept, dropped = hyp.universe_signals(universe, {"X": bars}, hyp.frozen_rules())
    assert kept.empty and dropped["reason"].str.startswith("overnight move beyond 25").all()


def test_bars_outside_the_registered_period_are_ignored() -> None:
    bars = pd.DataFrame({"date": pd.to_datetime(["2021-09-23", "2021-09-24", "2026-09-25"])})
    assert hyp.in_period(bars)["date"].dt.date.tolist() == [dt.date(2021, 9, 24)]


def test_collector_universe_option_uses_the_same_collector(monkeypatch) -> None:
    seen = {}

    def collect_all(series):
        seen["collected"] = series
        return prices.RunSummary()

    def missing_session_bars(series):
        seen["checked"] = series
        return {}

    monkeypatch.setattr(prices, "setup_logging", lambda: None)
    monkeypatch.setattr(prices, "init_db", lambda: None)
    monkeypatch.setattr(prices, "log_summary", lambda summary: None)
    monkeypatch.setattr(prices, "collect_all", collect_all)
    monkeypatch.setattr(prices, "missing_session_bars", missing_session_bars)
    assert prices.main(["--universe", hyp.UNIVERSE]) == 0
    assert len(seen["collected"]) == 45 and seen["checked"] is seen["collected"]
