"""Corporate-action adjusted prices and detection of unrecorded price gaps.

Raw prices in the database are never modified. Adjusted prices are derived on the fly:
every price dated before an action's ex_date is multiplied by that action's
price_factor, cumulatively across actions. Split and bonus factors also scale volume
inversely (more shares, same value traded); demergers leave volume unchanged.
"""

import dataclasses
import logging

import pandas as pd

from config.corporate_actions import (
    VOLUME_ADJUSTED_TYPES,
    CorporateAction,
    load_corporate_actions,
)
from storage.db import ACTION_COLUMNS, sync_corporate_actions

logger = logging.getLogger(__name__)

PRICE_COLUMNS = ("open", "high", "low", "close", "adj_close")
GAP_THRESHOLD = 0.25  # overnight moves beyond +/-25% are almost never organic


def actions_frame(actions: list[CorporateAction]) -> pd.DataFrame:
    """Convert CorporateAction objects to a DataFrame with the table's columns."""
    frame = pd.DataFrame([dataclasses.asdict(a) for a in actions], columns=ACTION_COLUMNS)
    frame["ex_date"] = pd.to_datetime(frame["ex_date"])
    return frame


def sync_actions_from_config() -> set[str]:
    """Load config/corporate_actions.yaml into the database.

    Returns the symbols whose actions changed; their derived data must be fully
    recomputed because the adjustment applies to all history before an ex-date.
    """
    return sync_corporate_actions(actions_frame(load_corporate_actions()))


def adjust_prices(prices: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of one symbol's prices adjusted for its corporate actions.

    Rows on or after an ex_date are unchanged by that action. With no actions the
    result equals the input.
    """
    adjusted = prices.copy()
    if actions.empty or adjusted.empty:
        return adjusted

    price_cols = [c for c in PRICE_COLUMNS if c in adjusted.columns]
    adjusted[price_cols] = adjusted[price_cols].astype(float)
    volume_factor = pd.Series(1.0, index=adjusted.index)
    for action in actions.itertuples(index=False):
        before = adjusted["date"] < pd.Timestamp(action.ex_date)
        adjusted.loc[before, price_cols] *= action.price_factor
        if action.action_type in VOLUME_ADJUSTED_TYPES:
            volume_factor[before] /= action.price_factor

    if "volume" in adjusted.columns and (volume_factor != 1.0).any():
        adjusted["volume"] = (adjusted["volume"] * volume_factor).round().astype("int64")
    return adjusted


def find_unrecorded_gaps(
    prices: pd.DataFrame, actions: pd.DataFrame, threshold: float = GAP_THRESHOLD
) -> pd.DataFrame:
    """Return overnight gaps (open vs prior close) beyond `threshold` with no recorded action.

    Result columns: date, prev_close, open, gap (fractional, e.g. -0.40).
    """
    df = prices.sort_values("date")
    gaps = pd.DataFrame(
        {
            "date": df["date"],
            "prev_close": df["close"].shift(),
            "open": df["open"],
        }
    )
    gaps["gap"] = gaps["open"] / gaps["prev_close"] - 1
    recorded = set(pd.to_datetime(actions["ex_date"])) if not actions.empty else set()
    flagged = gaps[(gaps["gap"].abs() > threshold) & ~gaps["date"].isin(recorded)]
    return flagged.reset_index(drop=True)


def warn_unrecorded_gaps(symbol: str, prices: pd.DataFrame, actions: pd.DataFrame) -> int:
    """Log a warning for each unrecorded large gap. Returns how many were found."""
    flagged = find_unrecorded_gaps(prices, actions)
    for row in flagged.itertuples(index=False):
        logger.warning(
            "%s: %+.1f%% overnight gap on %s (prev close %.2f -> open %.2f) with no "
            "corporate action recorded. If this is a split/bonus/demerger Yahoo did not "
            "adjust, add it to config/corporate_actions.yaml.",
            symbol,
            row.gap * 100,
            row.date.date(),
            row.prev_close,
            row.open,
        )
    return len(flagged)
