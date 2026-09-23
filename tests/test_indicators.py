from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.indicators as indicators
from storage import db


def synthetic_prices(days: int = 400, symbol: str = "TEST", seed: int = 7) -> pd.DataFrame:
    """Random-walk OHLCV on business days, deterministic for a given seed."""
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.015, days)))
    spread = close * rng.uniform(0.005, 0.02, days)
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": pd.bdate_range("2024-01-01", periods=days),
            "open": close + rng.normal(0, 1, days),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "adj_close": close,
            "volume": rng.integers(100_000, 1_000_000, days),
        }
    )


def real_infy() -> pd.DataFrame | None:
    """INFY prices from the real database, or None if it hasn't been collected."""
    try:
        prices = db.read_prices("INFY")
    except Exception:
        return None
    return prices if len(prices) >= 250 else None


PRICE_SOURCES = [
    pytest.param(lambda: synthetic_prices(), id="synthetic"),
    pytest.param(real_infy, id="real-INFY"),
]


def load(source) -> pd.DataFrame:
    prices = source()
    if prices is None:
        pytest.skip("no INFY prices in data/stock_intel.db; run collectors.prices first")
    return prices


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


# --- correctness checks (synthetic + real data) --------------------------------------


@pytest.mark.parametrize("source", PRICE_SOURCES)
def test_rsi_stays_within_0_and_100(source) -> None:
    result = indicators.compute_indicators(load(source))
    rsi = result["rsi_14"].dropna()
    assert len(rsi) == len(result) - indicators.RSI_LENGTH
    assert rsi.between(0, 100).all()
    assert result["rsi_14"].iloc[: indicators.RSI_LENGTH].isna().all()


@pytest.mark.parametrize("source", PRICE_SOURCES)
@pytest.mark.parametrize("length", [20, 50, 200])
def test_sma_matches_pandas_rolling_mean(source, length: int) -> None:
    prices = load(source).sort_values("date").reset_index(drop=True)
    result = indicators.compute_indicators(prices)
    expected = prices["close"].rolling(length).mean()

    pd.testing.assert_series_equal(
        result[f"sma_{length}"], expected, check_names=False, rtol=1e-10, atol=1e-8
    )
    assert result[f"sma_{length}"].first_valid_index() == length - 1


@pytest.mark.parametrize("source", PRICE_SOURCES)
def test_bollinger_middle_is_sma20_and_bands_are_symmetric(source) -> None:
    result = indicators.compute_indicators(load(source)).dropna(subset=["bb_middle"])
    np.testing.assert_allclose(result["bb_middle"], result["sma_20"], rtol=1e-10)
    np.testing.assert_allclose(
        result["bb_upper"] - result["bb_middle"], result["bb_middle"] - result["bb_lower"]
    )


def test_macd_hist_is_macd_minus_signal() -> None:
    result = indicators.compute_indicators(synthetic_prices()).dropna(subset=["macd_hist"])
    np.testing.assert_allclose(result["macd_hist"], result["macd"] - result["macd_signal"])


def test_short_history_leaves_long_indicators_empty() -> None:
    result = indicators.compute_indicators(synthetic_prices(days=60))
    assert len(result) == 60
    assert result["sma_200"].isna().all()
    assert result["sma_50"].notna().sum() == 11


# --- persistence and incremental runs ------------------------------------------------


def test_process_symbol_is_idempotent(engine: Engine) -> None:
    db.upsert_prices(synthetic_prices(), engine)
    indicators.process_symbol("TEST")
    indicators.process_symbol("TEST", full=True)
    assert len(db.read_indicators("TEST", engine=engine)) == 400


def test_incremental_run_matches_full_recompute(engine: Engine) -> None:
    prices = synthetic_prices(days=1000)
    db.upsert_prices(prices.iloc[:900], engine)
    indicators.process_symbol("TEST")

    db.upsert_prices(prices.iloc[900:], engine)
    assert indicators.process_symbol("TEST") == 101  # last stored date + 100 new bars

    stored = db.read_indicators("TEST", engine=engine)
    expected = indicators.compute_indicators(prices)
    cols = indicators.INDICATOR_COLUMNS
    assert len(stored) == 1000
    pd.testing.assert_frame_equal(
        stored[cols].reset_index(drop=True), expected[cols], rtol=1e-9, check_dtype=False
    )


def test_no_prices_writes_nothing(engine: Engine) -> None:
    assert indicators.process_symbol("MISSING") == 0
