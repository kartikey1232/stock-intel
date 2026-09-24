"""NSE trading calendar: weekdays minus the holidays in config/market_holidays.yaml."""

import datetime as dt
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = dt.time(15, 30)
# Yahoo's final daily bar can arrive after the close, so a session only counts as
# complete (for the missing-bar check and signals) from this time on.
FINAL_BAR_TIME = dt.time(16, 0)
DEFAULT_HOLIDAYS_PATH = Path(__file__).resolve().parent / "market_holidays.yaml"


class MarketCalendarError(ValueError):
    """Raised when the holidays file is missing or malformed."""


def load_holidays(path: Path = DEFAULT_HOLIDAYS_PATH) -> frozenset[dt.date]:
    """Load the list of weekday exchange holidays."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MarketCalendarError(f"{path}: {exc}") from exc
    days = raw.get("holidays") if isinstance(raw, dict) else None
    if not isinstance(days, list) or not all(isinstance(d, dt.date) for d in days):
        raise MarketCalendarError(f"{path}: expected a 'holidays' list of YYYY-MM-DD dates")
    return frozenset(days)


def is_trading_day(day: dt.date, holidays: frozenset[dt.date]) -> bool:
    """True if NSE trades on `day`: a weekday that isn't a listed holiday."""
    return day.weekday() < 5 and day not in holidays


def latest_completed_session(now: dt.datetime, holidays: frozenset[dt.date]) -> dt.date:
    """The most recent trading day whose final bar is due (16:00 IST, FINAL_BAR_TIME) at `now`.

    Logs a warning if the holidays file has no entries for that day's year, since every
    weekday then counts as a trading day.
    """
    local = now.astimezone(IST)
    day = local.date()
    if local.time() < FINAL_BAR_TIME:
        day -= dt.timedelta(days=1)
    while not is_trading_day(day, holidays):
        day -= dt.timedelta(days=1)
    if not any(h.year == day.year for h in holidays):
        logger.warning(
            "config/market_holidays.yaml lists no holidays for %d; add NSE's list", day.year
        )
    return day
