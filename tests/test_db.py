import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, func, select

from storage.db import (
    INDICATORS,
    PRICES,
    create_db_engine,
    init_db,
    read_indicators,
    read_prices,
    upsert_indicators,
    upsert_prices,
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    return engine


def count(engine: Engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def make_prices(symbol: str = "INFY", days: int = 5, start: str = "2026-09-01") -> pd.DataFrame:
    dates = pd.date_range(start, periods=days, freq="B")
    close = np.linspace(1000, 1040, days)
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": close - 5,
            "high": close + 10,
            "low": close - 10,
            "close": close,
            "adj_close": close,
            "volume": np.arange(days) * 1000 + 50_000,
        }
    )


def make_indicators(symbol: str = "INFY", days: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": pd.date_range("2026-09-01", periods=days, freq="B"),
            "rsi_14": [np.nan, np.nan, 55.0, 60.0, 65.0],
            "sma_20": [1000.0, 1001.0, 1002.0, 1003.0, 1004.0],
        }
    )


def test_upsert_prices_twice_does_not_duplicate(engine: Engine) -> None:
    df = make_prices()
    assert upsert_prices(df, engine) == 5
    assert upsert_prices(df, engine) == 5
    assert count(engine, PRICES) == 5


def test_upsert_indicators_twice_does_not_duplicate(engine: Engine) -> None:
    df = make_indicators()
    upsert_indicators(df, engine)
    upsert_indicators(df, engine)
    assert count(engine, INDICATORS) == 5


def test_upsert_updates_existing_rows_and_adds_new(engine: Engine) -> None:
    upsert_prices(make_prices(days=5), engine)
    revised = make_prices(days=7)  # 5 overlapping dates + 2 new
    revised["close"] = revised["close"] + 1
    upsert_prices(revised, engine)

    out = read_prices("INFY", engine=engine)
    assert len(out) == 7
    assert out["close"].tolist() == pytest.approx(revised["close"].tolist())


def test_symbols_are_kept_separate(engine: Engine) -> None:
    upsert_prices(make_prices("INFY"), engine)
    upsert_prices(make_prices("TCS"), engine)
    assert count(engine, PRICES) == 10
    assert len(read_prices("TCS", engine=engine)) == 5


def test_read_prices_filters_by_inclusive_date_range(engine: Engine) -> None:
    upsert_prices(make_prices(days=5), engine)  # 2026-09-01 .. 2026-09-07 (business days)
    out = read_prices("INFY", start="2026-09-02", end=dt.date(2026, 9, 4), engine=engine)
    assert out["date"].dt.date.tolist() == [
        dt.date(2026, 9, 2),
        dt.date(2026, 9, 3),
        dt.date(2026, 9, 4),
    ]


def test_round_trip_preserves_values_nulls_and_utc(engine: Engine) -> None:
    upsert_prices(make_prices(), engine)
    upsert_indicators(make_indicators(), engine)

    prices = read_prices("INFY", engine=engine)
    assert prices["volume"].iloc[0] == 50_000
    assert prices["fetched_at"].iloc[0].tzinfo == dt.UTC

    indicators = read_indicators("INFY", engine=engine)
    assert indicators["rsi_14"].isna().tolist() == [True, True, False, False, False]
    assert indicators["macd"].isna().all()  # column not supplied -> NULL


def test_tz_aware_dates_keep_exchange_calendar_date(engine: Engine) -> None:
    df = make_prices(days=1)
    df["date"] = pd.Timestamp("2026-09-01 00:00", tz="Asia/Kolkata")  # 2026-08-31 in UTC
    upsert_prices(df, engine)
    assert read_prices("INFY", engine=engine)["date"].dt.date.tolist() == [dt.date(2026, 9, 1)]


def test_read_unknown_symbol_returns_empty_frame(engine: Engine) -> None:
    out = read_prices("NOPE", engine=engine)
    assert out.empty
    assert "close" in out.columns


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda df: df.drop(columns="symbol"), "missing key column"),
        (lambda df: df.assign(clsoe=1.0), "unknown column"),
        (lambda df: pd.concat([df, df.iloc[[0]]]), "duplicate"),
    ],
)
def test_invalid_frames_raise(engine: Engine, mutate, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        upsert_prices(mutate(make_prices()), engine)
