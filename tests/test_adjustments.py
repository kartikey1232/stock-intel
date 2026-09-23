import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.indicators as indicators
from config.corporate_actions import (
    CorporateAction,
    CorporateActionError,
    load_corporate_actions,
)
from processing.adjustments import (
    actions_frame,
    adjust_prices,
    find_unrecorded_gaps,
    warn_unrecorded_gaps,
)
from storage import db

EX_DATE = pd.Timestamp("2025-10-14")


def prices_with_drop(drop: float = 0.40, days: int = 300, symbol: str = "TEST") -> pd.DataFrame:
    """Smooth random walk whose open on EX_DATE is `drop` below the prior close."""
    rng = np.random.default_rng(1)
    dates = pd.bdate_range(end=EX_DATE + pd.offsets.BDay(100), periods=days)
    close = 600 * np.exp(np.cumsum(rng.normal(0, 0.01, days)))
    close[dates >= EX_DATE] *= 1 - drop
    open_ = np.r_[close[0], close[:-1]]  # open at the prior close: no organic gaps
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": open_ * np.where(dates == EX_DATE, 1 - drop, 1.0),
            "high": np.maximum(open_, close) * 1.01,
            "low": np.minimum(open_, close) * 0.99,
            "close": close,
            "adj_close": close,
            "volume": np.full(days, 1_000_000),
        }
    )


def action(
    factor: float, action_type: str = "demerger", ex_date: pd.Timestamp = EX_DATE
) -> pd.DataFrame:
    return actions_frame(
        [CorporateAction("TEST", ex_date.date(), action_type, factor, "test", None)]
    )


def exact_factor(prices: pd.DataFrame) -> float:
    """Ex-date open / prior close, i.e. the factor that makes the series continuous."""
    i = prices.index[prices["date"] == EX_DATE][0]
    return prices.loc[i, "open"] / prices.loc[i - 1, "close"]


# --- adjust_prices -----------------------------------------------------------------


def test_prices_on_and_after_ex_date_are_untouched() -> None:
    raw = prices_with_drop()
    adjusted = adjust_prices(raw, action(exact_factor(raw)))
    after = raw["date"] >= EX_DATE
    pd.testing.assert_frame_equal(adjusted[after], raw[after].astype(adjusted.dtypes.to_dict()))


def test_ex_date_gap_disappears_in_adjusted_data() -> None:
    raw = prices_with_drop()
    actions = action(exact_factor(raw))
    assert len(find_unrecorded_gaps(raw, actions.iloc[0:0])) == 1  # raw has the gap

    adjusted = adjust_prices(raw, actions)
    assert find_unrecorded_gaps(adjusted, actions.iloc[0:0], threshold=0.01).empty
    i = adjusted.index[adjusted["date"] == EX_DATE][0]
    assert adjusted.loc[i, "open"] == pytest.approx(adjusted.loc[i - 1, "close"])


def test_prices_before_ex_date_are_scaled_by_factor() -> None:
    raw = prices_with_drop()
    adjusted = adjust_prices(raw, action(0.6))
    before = raw["date"] < EX_DATE
    for col in ("open", "high", "low", "close", "adj_close"):
        np.testing.assert_allclose(adjusted.loc[before, col], raw.loc[before, col] * 0.6)


def test_stock_without_actions_is_unaffected() -> None:
    raw = prices_with_drop(drop=0.0)
    pd.testing.assert_frame_equal(adjust_prices(raw, action(0.5).iloc[0:0]), raw)


def test_demerger_leaves_volume_unchanged() -> None:
    raw = prices_with_drop()
    adjusted = adjust_prices(raw, action(0.6, "demerger"))
    pd.testing.assert_series_equal(adjusted["volume"], raw["volume"])


@pytest.mark.parametrize("action_type", ["split", "bonus"])
def test_split_and_bonus_scale_volume_inversely(action_type: str) -> None:
    raw = prices_with_drop(drop=0.5)
    adjusted = adjust_prices(raw, action(0.5, action_type))
    before = raw["date"] < EX_DATE
    assert (adjusted.loc[before, "volume"] == 2_000_000).all()
    assert (adjusted.loc[~before, "volume"] == 1_000_000).all()


def test_multiple_actions_compound() -> None:
    raw = prices_with_drop(drop=0.0)
    early = EX_DATE - pd.offsets.BDay(50)
    actions = pd.concat([action(0.5, "split", early), action(0.8)], ignore_index=True)
    adjusted = adjust_prices(raw, actions)
    ratio = adjusted["close"] / raw["close"]
    assert ratio[raw["date"] < early].round(10).unique().tolist() == [0.4]
    assert ratio[(raw["date"] >= early) & (raw["date"] < EX_DATE)].round(10).unique() == [0.8]
    assert ratio[raw["date"] >= EX_DATE].round(10).unique().tolist() == [1.0]


# --- gap detection -----------------------------------------------------------------


def test_gap_detector_flags_unrecorded_40pct_jump() -> None:
    raw = prices_with_drop(drop=0.40)
    flagged = find_unrecorded_gaps(raw, action(0.6).iloc[0:0])
    assert flagged["date"].tolist() == [EX_DATE]
    assert flagged["gap"].iloc[0] == pytest.approx(-0.40, abs=0.02)


def test_gap_detector_ignores_recorded_jump() -> None:
    raw = prices_with_drop(drop=0.40)
    assert find_unrecorded_gaps(raw, action(0.6)).empty


def test_gap_detector_ignores_moves_below_threshold() -> None:
    assert find_unrecorded_gaps(prices_with_drop(drop=0.20), action(1).iloc[0:0]).empty


def test_gap_warning_names_symbol_and_date(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="processing.adjustments"):
        count = warn_unrecorded_gaps("TEST", prices_with_drop(), action(0.6).iloc[0:0])
    assert count == 1
    assert "TEST" in caplog.text and "2025-10-14" in caplog.text


# --- config loader -----------------------------------------------------------------


def test_real_corporate_actions_file_loads() -> None:
    actions = load_corporate_actions()
    tmpv = [a for a in actions if a.symbol == "TMPV"]
    assert len(tmpv) == 1
    assert tmpv[0].ex_date == dt.date(2025, 10, 14)
    assert tmpv[0].action_type == "demerger"
    assert tmpv[0].price_factor == pytest.approx(400 / 660.75)


VALID = """
  - symbol: TEST
    ex_date: 2025-10-14
    action_type: split
    price_factor: 0.5
    source: test
"""


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("actions:" + VALID.replace("split", "merger"), "action_type"),
        ("actions:" + VALID.replace("0.5", "0"), "price_factor"),
        ("actions:" + VALID.replace("0.5", "-1"), "price_factor"),
        ("actions:" + VALID.replace("2025-10-14", "yesterday"), "ex_date"),
        ("actions:" + VALID.replace("    source: test\n", ""), "missing field"),
        ("actions:" + VALID + VALID, "duplicate"),
        ("actions:" + VALID + "    factor: 2\n", "unknown field"),
    ],
)
def test_invalid_corporate_actions_raise(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "actions.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CorporateActionError, match=message):
        load_corporate_actions(path)


def test_empty_actions_file_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "actions.yaml"
    path.write_text("actions: []\n", encoding="utf-8")
    assert load_corporate_actions(path) == []


# --- database sync and end-to-end ----------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def test_sync_reports_changes_and_is_idempotent(engine: Engine) -> None:
    both = pd.concat([action(0.6), action(0.5).assign(symbol="OTHER")], ignore_index=True)
    assert db.sync_corporate_actions(both) == {"TEST", "OTHER"}
    assert db.sync_corporate_actions(both) == set()

    edited = both.copy()
    edited.loc[0, "price_factor"] = 0.61
    assert db.sync_corporate_actions(edited) == {"TEST"}

    assert db.sync_corporate_actions(edited.iloc[1:]) == {"TEST"}  # removed
    assert db.read_corporate_actions()["symbol"].tolist() == ["OTHER"]


def test_indicators_use_adjusted_prices(engine: Engine, caplog) -> None:
    raw = prices_with_drop()
    db.upsert_prices(raw, engine)
    db.sync_corporate_actions(action(exact_factor(raw)))

    with caplog.at_level(logging.WARNING):
        indicators.process_symbol("TEST", full=True)
    assert "no corporate action recorded" not in caplog.text

    stored = db.read_indicators("TEST", engine=engine)
    adjusted_close = adjust_prices(raw, action(exact_factor(raw)))["close"]
    np.testing.assert_allclose(
        stored["sma_50"].dropna(), adjusted_close.rolling(50).mean().dropna(), rtol=1e-10
    )
    # With the gap removed, RSI never collapses the way it would on raw prices.
    assert stored["rsi_14"].min() > 15


def test_unrecorded_gap_is_warned_during_indicator_step(engine: Engine, caplog) -> None:
    db.upsert_prices(prices_with_drop(), engine)
    with caplog.at_level(logging.WARNING):
        indicators.process_symbol("TEST", full=True)
    assert "TEST" in caplog.text and "2025-10-14" in caplog.text
