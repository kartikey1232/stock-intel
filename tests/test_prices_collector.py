import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

import collectors.prices as prices
from config.loader import Stock
from storage import db
from utils.retry import retry

TODAY = dt.date(2026, 9, 23)


def stock(symbol: str) -> Stock:
    return Stock(symbol=symbol, yf=f"{symbol}.NS", name=symbol, sector="Test", aliases=(symbol,))


def yahoo_frame(ticker: str, dates: list[str], tz: str | None = None) -> pd.DataFrame:
    """Mimic yf.download output: MultiIndex (Price, Ticker) columns."""
    index = pd.DatetimeIndex(pd.to_datetime(dates), name="Date")
    if tz:
        index = index.tz_localize(tz)
    close = np.linspace(100, 110, len(dates))
    data = {
        "Adj Close": close,
        "Close": close,
        "High": close + 1,
        "Low": close - 1,
        "Open": close,
        "Volume": np.full(len(dates), 1000.0),
    }
    df = pd.DataFrame(data, index=index)
    df.columns = pd.MultiIndex.from_product([df.columns, [ticker]], names=["Price", "Ticker"])
    return df


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(prices.time, "sleep", lambda _s: None)
    return engine


# --- normalise_ohlcv ---------------------------------------------------------------


def test_normalise_flattens_multiindex_and_renames() -> None:
    df = prices.normalise_ohlcv(yahoo_frame("INFY.NS", ["2026-09-21", "2026-09-22"]), "INFY")
    assert list(df.columns) == [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
    assert df["symbol"].unique().tolist() == ["INFY"]
    assert df["volume"].dtype == "int64"


def test_normalise_converts_tz_aware_index_to_ist_date() -> None:
    # 18:30 UTC on the 21st is midnight IST on the 22nd.
    raw = yahoo_frame("INFY.NS", ["2026-09-21 18:30"], tz="UTC")
    df = prices.normalise_ohlcv(raw, "INFY")
    assert df["date"].dt.date.tolist() == [dt.date(2026, 9, 22)]


def test_normalise_drops_rows_without_prices_and_duplicate_dates() -> None:
    raw = yahoo_frame("INFY.NS", ["2026-09-21", "2026-09-22", "2026-09-22", "2026-09-23"])
    raw.iloc[3, :5] = np.nan  # a holiday row with no OHLC
    df = prices.normalise_ohlcv(raw, "INFY")
    assert df["date"].dt.date.tolist() == [dt.date(2026, 9, 21), dt.date(2026, 9, 22)]


def test_normalise_drops_flat_zero_volume_holiday_bars() -> None:
    raw = yahoo_frame("INFY.NS", ["2026-04-30", "2026-05-01", "2026-05-04"])
    raw.iloc[1, :5] = 105.0  # flat OHLC (and Adj Close)
    raw.iloc[1, 5] = 0  # zero volume
    df = prices.normalise_ohlcv(raw, "INFY")
    assert dt.date(2026, 5, 1) not in df["date"].dt.date.tolist()
    assert len(df) == 2


def test_normalise_empty_frame_returns_empty() -> None:
    assert prices.normalise_ohlcv(pd.DataFrame(), "INFY").empty


# --- fetch_start_date --------------------------------------------------------------


def test_first_run_fetches_five_years() -> None:
    assert prices.fetch_start_date(None, TODAY) == TODAY - dt.timedelta(days=365 * 5)


def test_incremental_run_starts_at_last_stored_date() -> None:
    last = dt.date(2026, 9, 18)
    assert prices.fetch_start_date(last, TODAY) == last


# --- collect_all -------------------------------------------------------------------


def test_one_failure_does_not_stop_others(engine: Engine, monkeypatch) -> None:
    def fake_download(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        if ticker == "BAD.NS":
            raise ConnectionError("boom")
        return yahoo_frame(ticker, ["2026-09-21", "2026-09-22"])

    monkeypatch.setattr(prices, "download_ohlcv", fake_download)
    summary = prices.collect_all([stock("INFY"), stock("BAD"), stock("TCS")])

    assert summary.rows_written == {"INFY": 2, "TCS": 2}
    assert "ConnectionError" in summary.failures["BAD"]
    assert db.count_prices() == {"INFY": 2, "TCS": 2}


def test_empty_first_run_is_a_failure(engine: Engine, monkeypatch) -> None:
    monkeypatch.setattr(prices, "download_ohlcv", lambda *a: pd.DataFrame())
    summary = prices.collect_all([stock("GONE")])
    assert "NoDataError" in summary.failures["GONE"]


def test_rerun_is_incremental_and_idempotent(engine: Engine, monkeypatch) -> None:
    calls: list[dt.date] = []

    def fake_download(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        calls.append(start)
        return yahoo_frame(ticker, ["2026-09-21", "2026-09-22"])

    monkeypatch.setattr(prices, "download_ohlcv", fake_download)
    prices.collect_all([stock("INFY")])
    prices.collect_all([stock("INFY")])

    assert calls[1] == dt.date(2026, 9, 22)  # second run starts from last stored date
    assert db.count_prices() == {"INFY": 2}


def test_empty_incremental_run_is_not_a_failure(engine: Engine, monkeypatch) -> None:
    monkeypatch.setattr(prices, "download_ohlcv", lambda t, s, e: yahoo_frame(t, ["2026-09-18"]))
    prices.collect_all([stock("INFY")])
    monkeypatch.setattr(prices, "download_ohlcv", lambda *a: pd.DataFrame())
    summary = prices.collect_all([stock("INFY")])
    assert summary.rows_written == {"INFY": 0}
    assert not summary.failures


# --- retry -------------------------------------------------------------------------


def test_retry_backs_off_then_succeeds() -> None:
    delays: list[float] = []
    attempts = iter([ConnectionError("1"), ConnectionError("2"), "ok"])

    @retry(attempts=3, base_delay=1.0, sleep=delays.append)
    def flaky() -> str:
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    assert flaky() == "ok"
    assert len(delays) == 2
    assert 1.0 <= delays[0] <= 1.25 and 2.0 <= delays[1] <= 2.5


def test_retry_reraises_after_last_attempt() -> None:
    @retry(attempts=2, base_delay=0, sleep=lambda _s: None)
    def always_fails() -> None:
        raise TimeoutError("nope")

    with pytest.raises(TimeoutError):
        always_fails()
