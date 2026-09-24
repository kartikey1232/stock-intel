"""Tests for rule-based price signals, above all that they never use future data."""

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.signals as sig
from config.signals import DEFAULT_SIGNALS_PATH, SignalConfigError, SignalRule, load_signals
from processing.adjustments import adjust_prices
from storage import db

RULES = load_signals()


def random_bars(n: int = 900, seed: int = 7) -> pd.DataFrame:
    """A random walk with volatility regimes, volume bursts and occasional gaps, so every
    signal type fires somewhere."""
    rng = np.random.default_rng(seed)
    vol = np.where((np.arange(n) // 150) % 2, 0.025, 0.012)
    drift = np.where((np.arange(n) // 300) % 2, -0.0015, 0.002)  # up, down, up trends
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol)))
    gaps = rng.random(n) < 0.02
    open_ = np.roll(close, 1) * (1 + np.where(gaps, rng.choice([-0.05, 0.05], n), 0))
    open_[0] = close[0]
    high = np.maximum(open_, close) * (1 + rng.random(n) * 0.01)
    low = np.minimum(open_, close) * (1 - rng.random(n) * 0.01)
    volume = rng.integers(1_000_000, 2_000_000, n) * np.where(rng.random(n) < 0.03, 3, 1)
    return pd.DataFrame(
        {
            "symbol": "TEST",
            "date": pd.bdate_range("2021-01-04", periods=n),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "adj_close": close,
            "volume": volume.astype("int64"),
        }
    )


def rule(name: str) -> SignalRule:
    return next(r for r in RULES if r.name == name)


# --- look-ahead --------------------------------------------------------------------


@pytest.mark.parametrize("cut", [260, 420, 611, 780])
def test_signals_never_change_when_future_bars_are_appended(cut: int) -> None:
    bars = random_bars()
    full = sig.compute_signals(bars, RULES)
    assert set(full["signal"]) == {r.name for r in RULES}  # every rule is exercised
    past = sig.compute_signals(bars.iloc[:cut], RULES)
    cutoff = bars["date"].iloc[cut - 1]
    pd.testing.assert_frame_equal(
        past.reset_index(drop=True),
        full[full["date"] <= cutoff].reset_index(drop=True),
        check_dtype=False,
    )


def test_each_days_signals_are_known_at_that_days_close() -> None:
    # Replay history bar by bar: the signals computed when a day is the newest bar must
    # equal the full-history signals for that day. Any rule peeking even one bar ahead
    # fails here, including on days where only the future would make it fire.
    bars = random_bars(700)
    full = sig.compute_signals(bars, RULES)
    by_day = {day: g.drop(columns="date").reset_index(drop=True) for day, g in full.groupby("date")}
    empty = full.iloc[0:0].drop(columns="date").reset_index(drop=True)
    for end in range(2, len(bars) + 1):
        now = sig.compute_signals(bars.iloc[:end], RULES)
        today = bars["date"].iloc[end - 1]
        latest = now[now["date"] == today].drop(columns="date").reset_index(drop=True)
        pd.testing.assert_frame_equal(latest, by_day.get(today, empty), check_dtype=False,
                                      obj=f"signals on {today.date()}")  # fmt: skip


def test_a_later_corporate_action_does_not_change_earlier_signals() -> None:
    bars = random_bars()
    cut = 600
    ex_date = bars["date"].iloc[cut + 50]
    split = pd.DataFrame(
        {"ex_date": [ex_date], "action_type": ["split"], "price_factor": [0.2]}
    )  # a 1:5 split after the cut rescales every earlier bar
    before = sig.compute_signals(bars.iloc[:cut], RULES)
    after = sig.compute_signals(adjust_prices(bars, split), RULES)
    after = after[after["date"] <= bars["date"].iloc[cut - 1]].reset_index(drop=True)
    pd.testing.assert_frame_equal(before, after, check_dtype=False, rtol=1e-9)


def test_intraday_bar_is_excluded() -> None:
    bars = random_bars(30)
    last_complete = bars["date"].iloc[-2].date()
    assert sig.completed_bars(bars, last_complete)["date"].max().date() == last_complete


# --- individual rules --------------------------------------------------------------


def flat_bars(closes: list[float], volumes: list[int] | None = None) -> pd.DataFrame:
    n = len(closes)
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=n),
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": volumes or [1000] * n,
        }
    )


def test_golden_and_death_cross() -> None:
    closes = [100.0] * 200 + [90.0] * 60 + [140.0] * 120 + [60.0] * 150
    out = sig.compute_signals(flat_bars(closes), [rule("golden_cross"), rule("death_cross")])
    kinds = out["signal"].tolist()
    assert kinds.count("golden_cross") == 1 and kinds.count("death_cross") >= 1
    golden = out[out["signal"] == "golden_cross"].iloc[0]
    assert golden["direction"] == "bullish" and golden["value"] > 0


def test_volume_spike_uses_the_prior_average_and_the_days_move() -> None:
    closes = [100.0] * 25 + [95.0]
    volumes = [1000] * 25 + [2500]
    out = sig.compute_signals(flat_bars(closes, volumes), [rule("volume_spike")])
    assert len(out) == 1
    assert out.iloc[0]["value"] == pytest.approx(2.5)
    assert out.iloc[0]["direction"] == "bearish"
    # Exactly 2x isn't "more than 2x".
    exact = flat_bars(closes, [1000] * 25 + [2000])
    assert sig.compute_signals(exact, [rule("volume_spike")]).empty


def test_gap_threshold_and_52_week_breakout_cooldown() -> None:
    closes = [100.0] * 260 + [101.0 + i for i in range(30)]
    bars = flat_bars(closes)
    bars.loc[260, "open"] = 104.5  # 4.5% above the previous close
    out = sig.compute_signals(bars, [rule("gap_up"), rule("high_52w_breakout")])
    assert out[out["signal"] == "gap_up"]["value"].tolist() == [pytest.approx(4.5)]
    highs = out[out["signal"] == "high_52w_breakout"]["date"]
    assert len(highs) == 2  # 30 consecutive new highs, cooldown 20: days 0 and 21
    assert (highs.iloc[1] - highs.iloc[0]).days > 20


def test_rsi_crossings() -> None:
    closes = [100.0 + (i % 2) for i in range(40)] + [100.0 - 2 * i for i in range(1, 15)]
    out = sig.compute_signals(flat_bars(closes), [rule("rsi_below_30")])
    assert len(out) == 1 and out.iloc[0]["value"] < 30


def test_nothing_fires_during_warm_up() -> None:
    assert sig.compute_signals(random_bars(150), [rule("golden_cross")]).empty


# --- storage and config ------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def test_process_symbol_replaces_stored_signals(engine: Engine) -> None:
    db.upsert_prices(random_bars().assign(fetched_at=pd.Timestamp("2026-09-24", tz="UTC")))
    now = dt.datetime(2026, 9, 24, tzinfo=dt.UTC)
    n = sig.process_symbol("TEST", RULES, dt.date(2030, 1, 1), now)
    assert n > 0 and sig.process_symbol("TEST", RULES, dt.date(2030, 1, 1), now) == n
    stored = db.read_signals("TEST")
    assert len(stored) == n and set(stored["direction"]) <= {"bullish", "bearish", "neutral"}
    counts = sig.counts_table(stored, RULES)
    assert list(counts.index) == [r.name for r in RULES] and counts["total"].sum() == n


def test_project_signals_config_is_valid() -> None:
    rules = load_signals(DEFAULT_SIGNALS_PATH)
    assert {r.type for r in rules} == set(sig.RULES)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("x: {type: ma_cross, fast: 200, slow: 50, cross: above, direction: bullish}", "shorter"),
        ("x: {type: gap, threshold_pct: 3, side: sideways, direction: bullish}", "side"),
        ("x: {type: rsi_cross, length: 14, level: 130, cross: above, direction: bearish}", "0"),
        ("x: {type: volume_spike, window: 20, multiple: 2, direction: up}", "direction"),
        ("x: {type: gap, threshold_pct: 3, side: up, direction: bullish, colour: red}", "unknown"),
        ("x: {type: wave, direction: bullish}", "type"),
    ],
)
def test_invalid_signal_definitions_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "signals.yaml"
    path.write_text(f"signals:\n  {body}\n", encoding="utf-8")
    with pytest.raises(SignalConfigError, match=message):
        load_signals(path)
