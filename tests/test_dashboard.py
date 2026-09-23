import datetime as dt

import numpy as np
import pandas as pd
import pytest

import dashboard
from processing.indicators import compute_indicators


def history(days: int = 300, last_close: float = 110.0) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-09-23", periods=days)
    close = np.linspace(100, last_close, days)
    prices = pd.DataFrame(
        {
            "symbol": "TEST",
            "date": dates,
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 1000,
        }
    )
    return dashboard.merge_prices_indicators(prices, compute_indicators(prices))


def test_metrics_use_latest_day_and_52_week_window() -> None:
    df = history()
    df.loc[0, "high"] = 999.0  # > 52 weeks before the last date: must be ignored
    m = dashboard.compute_metrics(df)

    assert m.as_of == dt.date(2026, 9, 23)
    assert m.last_close == pytest.approx(110.0)
    expected_change = (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
    assert m.day_change_pct == pytest.approx(expected_change)
    assert m.high_52w == pytest.approx(112.0)
    assert m.rsi == pytest.approx(df["rsi_14"].iloc[-1])
    assert m.above_sma_200 is True


def test_metrics_below_sma_200() -> None:
    df = history()
    df.loc[df.index[-1], "close"] = 50.0
    assert dashboard.compute_metrics(df).above_sma_200 is False


def test_metrics_handle_missing_indicators() -> None:
    df = history(days=30)
    m = dashboard.compute_metrics(df)
    assert m.sma_200 is None and m.above_sma_200 is None


def test_filter_range_is_inclusive() -> None:
    df = history(days=10)
    out = dashboard.filter_range(df, dt.date(2026, 9, 21), dt.date(2026, 9, 23))
    assert out["date"].dt.date.tolist() == [dt.date(2026, 9, d) for d in (21, 22, 23)]


def test_missing_trading_days_finds_weekday_holidays() -> None:
    dates = pd.Series(pd.to_datetime(["2026-04-30", "2026-05-04"]))  # 1 May holiday, weekend
    assert dashboard.missing_trading_days(dates) == ["2026-05-01"]


def test_build_figure_with_and_without_indicators() -> None:
    df = history()
    full = dashboard.build_figure(df, "TEST")
    names = {t.name for t in full.data}
    assert {"Price", "SMA 50", "SMA 200", "BB upper", "BB lower", "RSI", "MACD"} <= names

    prices_only = df[["symbol", "date", "open", "high", "low", "close", "volume"]]
    bare = dashboard.build_figure(prices_only, "TEST")
    assert {t.name for t in bare.data} == {"Price", "Volume"}
