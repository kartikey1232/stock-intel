import datetime as dt
from pathlib import Path

import pytest

from config.market_calendar import (
    IST,
    MarketCalendarError,
    is_trading_day,
    latest_completed_session,
    load_holidays,
)

HOLIDAYS = frozenset({dt.date(2026, 9, 14)})  # Monday


def at(day: str, hhmm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=IST)


def test_real_holidays_file_loads() -> None:
    holidays = load_holidays()
    assert dt.date(2026, 1, 26) in holidays
    assert all(is_trading_day(d, frozenset()) for d in holidays)  # only weekdays listed


def test_malformed_holidays_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "h.yaml"
    path.write_text("holidays: [not-a-date]\n")
    with pytest.raises(MarketCalendarError):
        load_holidays(path)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at("2026-09-23", "16:15"), "2026-09-23"),  # after the final bar: today
        (at("2026-09-23", "16:00"), "2026-09-23"),  # 16:00 itself counts
        (at("2026-09-23", "15:45"), "2026-09-22"),  # closed, but Yahoo's bar may be late
        (at("2026-09-23", "15:30"), "2026-09-22"),  # the close alone isn't enough
        (at("2026-09-23", "10:00"), "2026-09-22"),  # before close: yesterday
        (at("2026-09-19", "12:00"), "2026-09-18"),  # Saturday: Friday
        (at("2026-09-14", "17:00"), "2026-09-11"),  # holiday Monday: previous Friday
        (at("2026-09-15", "09:00"), "2026-09-11"),  # morning after the holiday
    ],
)
def test_latest_completed_session(now: dt.datetime, expected: str) -> None:
    assert latest_completed_session(now, HOLIDAYS) == dt.date.fromisoformat(expected)


def test_utc_time_is_converted_to_ist() -> None:
    now = dt.datetime(2026, 9, 23, 10, 45, tzinfo=dt.UTC)  # 16:15 IST
    assert latest_completed_session(now, HOLIDAYS) == dt.date(2026, 9, 23)


def test_year_without_holidays_warns(caplog: pytest.LogCaptureFixture) -> None:
    latest_completed_session(at("2027-01-05", "16:00"), HOLIDAYS)
    assert "no holidays for 2027" in caplog.text
