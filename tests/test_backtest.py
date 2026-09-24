"""Tests for the signal event study (synthetic prices; no network, no real database)."""

import numpy as np
import pandas as pd
import pytest

import processing.backtest as bt
from config.backtest import DEFAULT_BACKTEST_PATH, BacktestConfig, load_backtest_config

CONFIG = BacktestConfig(horizons=(1, 5), benchmark="NIFTY50", cluster_gap=10, min_events=30,
                        bootstrap_samples=2000, seed=1)  # fmt: skip


def bars(closes: list[float], opens: list[float] | None = None, start: str = "2024-01-01"):
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.bdate_range(start, periods=len(closes)),
            "open": opens if opens is not None else close.shift(1).fillna(close.iloc[0]),
            "high": close,
            "low": close,
            "close": close,
        }
    )


def test_returns_start_at_the_next_open_and_match_the_benchmark_window() -> None:
    stock = bars([100, 101, 102, 103, 104, 105], opens=[99, 110, 102, 103, 104, 105])
    bench = bars([50, 50, 51, 52, 53, 55])
    panel = bt.excess_panel(stock, bench, (1, 2))
    first = panel.iloc[0]
    assert first["entry_date"] == stock["date"][1]
    assert first["ret_1"] == pytest.approx(101 / 110 - 1)  # open(t+1) -> close(t+1)
    assert first["ret_2"] == pytest.approx(102 / 110 - 1)
    assert first["bench_2"] == pytest.approx(51 / 50 - 1)  # benchmark open(t+1) -> close(t+2)
    assert first["excess_2"] == pytest.approx(first["ret_2"] - first["bench_2"])
    assert panel["ret_1"].isna().tolist()[-1] and panel["ret_2"].isna().tolist()[-2:] == [True] * 2


def test_consecutive_firings_count_once() -> None:
    events = pd.DataFrame({"symbol": "X", "signal": "rsi_below_30",
                           "date": pd.bdate_range("2024-01-01", periods=5)})  # fmt: skip
    positions = pd.Series([0, 1, 2, 30, 45])
    keep = bt.first_in_clusters(events, positions, gap=10)
    assert keep.tolist() == [True, False, False, True, True]
    chained = bt.first_in_clusters(events, pd.Series([0, 8, 16, 24, 40]), gap=10)
    assert chained.tolist() == [True, False, False, False, True]  # a chain is one cluster


def study(signal_days: list[int], jump: float, direction: str = "bullish", drift: float = 0.0):
    """A stock that tracks a flat-ish benchmark, plus `drift` a day, and moves `jump`
    on the day after each signal day."""
    n = 1200
    rng = np.random.default_rng(3)
    bench_close = 100 * np.cumprod(1 + rng.normal(0, 0.005, n))
    noise = rng.normal(0, 0.002, n)
    stock_ret = bench_close / np.roll(bench_close, 1) - 1 + noise + drift
    stock_ret[0] = 0
    for day in signal_days:
        stock_ret[day + 2] += jump  # the day after the entry open
    stock_close = 100 * np.cumprod(1 + stock_ret)
    stock, bench = bars(list(stock_close)), bars(list(bench_close))
    signals = pd.DataFrame(
        {
            "symbol": "X",
            "date": stock["date"][signal_days].dt.date,
            "signal": "test_signal",
            "direction": direction,
            "value": 1.0,
        }
    )
    panels = {"X": bt.excess_panel(stock, bench, CONFIG.horizons)}
    events = bt.build_events(signals, panels, CONFIG)
    return bt.summarise(events, CONFIG, ["test_signal"]).set_index("horizon"), events


def test_a_real_post_signal_move_shows_up_as_an_edge() -> None:
    days = list(range(30, 1150, 25))  # 45 events, far enough apart not to cluster
    results, _ = study(days, jump=0.03)
    five = results.loc[5]
    assert five["n"] == len(days)
    # The baseline uses all days, events included (45 jumps x 5 windows / ~1195 days ~ 0.6%),
    # so the measured edge is a little below the planted 3%: conservative, not inflated.
    assert 0.02 < five["edge"] < 0.03
    assert five["ci_low"] > 0 and five["note"] == "beats baseline (CI > 0)"
    assert five["hit_rate"] > 0.9


def test_bearish_signals_work_when_the_stock_falls() -> None:
    results, _ = study(list(range(30, 1150, 25)), jump=-0.03, direction="bearish")
    assert results.loc[5, "edge"] > 0.02 and results.loc[5, "mean_excess"] < 0


def test_the_baseline_removes_a_stocks_general_outperformance() -> None:
    # The stock beats the benchmark by 0.2% every day, signal or not: no edge.
    results, _ = study(list(range(30, 1150, 25)), jump=0.0, drift=0.002)
    assert results.loc[5, "mean_excess"] > 0.007  # raw excess looks great...
    # ...but it's the baseline. (Across 40 noise seeds the mean edge here is 0.0000 with
    # sd 0.07%; this seed's draw is about -0.2%.)
    assert results.loc[5, "edge"] == pytest.approx(0.0, abs=0.003)
    assert abs(results.loc[5, "edge"]) < results.loc[5, "mean_excess"] / 3


def test_few_events_are_flagged() -> None:
    results, _ = study([40, 90, 140], jump=0.05)
    assert (results["note"] == bt.TOO_FEW).all() and (results["n"] == 3).all()


def test_bootstrap_is_reproducible() -> None:
    values = np.random.default_rng(0).normal(0.01, 0.02, 100)
    assert bt.bootstrap_ci(values, 2000, 5) == bt.bootstrap_ci(values, 2000, 5)
    low, high = bt.bootstrap_ci(values, 2000, 5)
    assert low < values.mean() < high


def test_report_states_the_limits() -> None:
    results, events = study(list(range(30, 1150, 25)), jump=0.03)
    report = bt.format_report(results.reset_index(), events, events)
    for phrase in ("survivorship bias", "by chance", "No transaction costs", "test_signal 45->45"):
        assert phrase in report


def test_project_backtest_config() -> None:
    config = load_backtest_config(DEFAULT_BACKTEST_PATH)
    assert config.horizons == (1, 5, 20, 60) and config.min_events == 30
