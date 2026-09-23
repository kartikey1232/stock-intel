import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.filing_categories as fc
from config.corporate_actions import CorporateAction
from storage import db

# (exchange category, subject, description, expected type, expected decided actions)
CASES = [
    # --- traps: nothing decided yet ---
    ("Board Meeting", "Board Meeting Intimation for Considering Bonus Issue of Equity Shares",
     None, "board_meeting", ()),
    ("Board Meeting", "Intimation of Board Meeting to consider buyback proposal",
     "The Board will consider a proposal for buyback of fully paid-up equity shares.",
     "board_meeting", ()),
    ("Board Meeting", "Board meeting on 23 July 2026 to consider financial results and dividend",
     None, "board_meeting", ()),
    ("Board Meeting", "Prior intimation of meeting to consider sub-division of shares",
     None, "board_meeting", ()),
    # --- traps: words that look like actions but aren't ---
    ("General Updates", "Annual bonus payout to employees under performance incentive plan",
     None, "other", ()),
    ("Change in Management", "Split of the roles of Chairman and Managing Director",
     None, "other", ()),
    ("Company Update", "Dividend Distribution Policy", None, "other", ()),
    ("Allotment of Securities", "Allotment of ESOP / ESPS", None, "other", ()),
    # --- decided corporate actions ---
    ("Outcome of Board Meeting", "Outcome of Board Meeting - Recommendation of Bonus Issue",
     "The Board recommended bonus equity shares in the ratio of 1:1, subject to approval.",
     "corporate_action", ("bonus",)),
    ("Company Update", "Record Date for Bonus Issue",
     "The Company has fixed Tuesday, October 28, 2025 as the Record Date for the bonus issue.",
     "corporate_action", ("bonus",)),
    ("Corp. Action", "Sub-division of equity shares",
     "Sub-division of 1 equity share of face value of Rs 10 each into 5 equity shares of face "
     "value of Rs 2 each. Record date: 14/11/2025.", "corporate_action", ("split",)),
    ("Scheme of Arrangement", "Scheme of Arrangement - Demerger of CV business - Record Date",
     "Record Date fixed as Tuesday, 14th October 2025 for 1 (one) fully paid-up equity share "
     "of TMCV for every 1 (one) equity share held.", "corporate_action", ("demerger",)),
    ("Rights Issue", "Rights issue of equity shares",
     "Rights issue of 1 rights equity share for every 15 equity shares held at Rs 1,257. "
     "Record date: 12-05-2026.", "corporate_action", ("rights",)),
    ("Buy back", "Post Buyback Public Announcement", None, "corporate_action", ("buyback",)),
    # --- other types ---
    ("Corp. Action", "Record date for interim dividend",
     "The Board declared an interim dividend of Rs 22 per share. Record date is 24th October, "
     "2025.", "dividend", ()),
    ("Result", "Financial Results for the quarter ended June 30, 2026", None, "results", ()),
    (None, "Integrated Filing- Financials", None, "results", ()),
    ("Outcome of Board Meeting", "Outcome of Board Meeting - Financial results and dividend",
     "Approved audited financial results; declared interim dividend of Rs 11.", "results", ()),
    ("Company Update", "Shareholding Pattern for the quarter ended September 30, 2026", None,
     "shareholding_pattern", ()),
    ("Company Update", "Closure of Trading Window",
     "Trading window closed in terms of SEBI (PIT) Regulations", "insider_trading", ()),
    ("Analysts/Institutional Investor Meet/Con. Call Updates", "Schedule of analyst meet", None,
     "analyst_meet", ()),
    ("Credit Rating", "CRISIL reaffirms AAA/Stable rating on bank's bonds", None,
     "credit_rating", ()),
    ("Press Release", "Press release - Infosys signs deal with European bank", None,
     "press_release", ()),
]  # fmt: skip


@pytest.mark.parametrize(("category", "subject", "description", "expected", "actions"), CASES)
def test_classification(category, subject, description, expected, actions) -> None:
    c = fc.classify(category, subject, description)
    assert c.filing_type == expected
    assert c.actions == actions
    assert c.filing_type in fc.FILING_TYPES


def test_combined_outcome_keeps_every_topic_as_tags() -> None:
    c = fc.classify(
        "Outcome of Board Meeting",
        "Outcome - financial results, interim dividend and bonus issue",
        "Approved financial results, declared dividend and bonus shares in the ratio of 1:2.",
    )
    assert c.filing_type == "corporate_action"
    assert {"results", "dividend", "corporate_action", "board_meeting"} <= set(c.tags)


# --- extraction --------------------------------------------------------------------


def extract(action: str, subject: str, description: str) -> fc.Action:
    return fc.extract_action(action, fc.filing_text(None, subject, description))


@pytest.mark.parametrize(
    ("text", "ratio", "factor"),
    [
        ("bonus equity shares in the ratio of 1:1", "1:1", 0.5),
        ("1:2 bonus issue approved", "1:2", 2 / 3),
        ("two (2) fully paid-up bonus equity shares for every one (1) share held", "2:1", 1 / 3),
        ("one bonus share for every ten shares", "1:10", 10 / 11),
    ],
)
def test_bonus_ratio_and_price_factor(text: str, ratio: str, factor: float) -> None:
    a = extract("bonus", "Bonus issue", text)
    assert a.ratio == ratio
    assert a.price_factor == pytest.approx(factor)


def test_split_from_face_values() -> None:
    a = extract("split", "Sub-division", "face value of Rs 10 each into shares of face value of ₹2")
    assert (a.ratio, a.price_factor) == ("1:5", pytest.approx(0.2))


@pytest.mark.parametrize(
    ("text", "date"),
    [
        ("Record date is 24th October, 2025.", dt.date(2025, 10, 24)),
        ("record date: 14/11/2025", dt.date(2025, 11, 14)),
        ("Record Date fixed as Tuesday, 14th October 2025 for", dt.date(2025, 10, 14)),
        ("fixed Tuesday, October 28, 2025 as the Record Date", dt.date(2025, 10, 28)),
        ("Record date: 12-05-2026", dt.date(2026, 5, 12)),  # day-first
    ],
)
def test_record_date_formats(text: str, date: dt.date) -> None:
    a = extract("bonus", "Bonus issue", text)
    assert a.record_date == date
    assert a.ex_date == date  # T+1: ex-date equals record date when not stated
    assert "T+1" in a.notes[0]


def test_explicit_ex_date_wins() -> None:
    a = extract("split", "Split", "Record date: 14/11/2025. Ex-date: 13/11/2025.")
    assert (a.record_date, a.ex_date, a.notes) == (
        dt.date(2025, 11, 14),
        dt.date(2025, 11, 13),
        [],
    )


def test_rights_and_demerger_have_ratio_but_no_price_factor() -> None:
    rights = extract("rights", "Rights issue", "1 rights equity share for every 15 held")
    demerger = extract("demerger", "Demerger", "1 (one) equity share of TMCV for every 1 (one)")
    assert (rights.ratio, rights.price_factor) == ("1:15", None)
    assert (demerger.ratio, demerger.price_factor) == ("1:1", None)


# --- status against the YAML and prices ----------------------------------------------

TODAY = dt.date(2026, 9, 23)


def prices_around(ex_date: dt.date, gap: float) -> pd.DataFrame:
    """Flat prices with an overnight move of `gap` on `ex_date`."""
    dates = pd.bdate_range(ex_date - dt.timedelta(days=10), ex_date + dt.timedelta(days=10))
    close = np.where(dates.date >= ex_date, 100 * (1 + gap), 100.0)
    return pd.DataFrame({"date": dates, "open": close, "close": close})


def action(ex_date: dt.date | None, kind: str = "bonus", factor: float | None = 0.5) -> fc.Action:
    return fc.Action(kind, "1:1", factor, ex_date, ex_date)


YAML = [CorporateAction("TMPV", dt.date(2025, 10, 14), "demerger", 0.6054, "NSE", None)]


@pytest.mark.parametrize(
    ("act", "symbol", "prices", "status"),
    [
        (action(dt.date(2025, 10, 14), "demerger", None), "TMPV", None, "recorded"),
        (action(dt.date(2025, 10, 15), "demerger", None), "TMPV", None, "recorded"),  # ±3 days
        (action(dt.date(2026, 10, 28)), "INFY", None, "upcoming"),
        (action(None), "INFY", None, "undated"),
        (action(dt.date(2026, 5, 12)), "INFY", prices_around(dt.date(2026, 5, 12), 0.0),
         "yahoo_adjusted"),
        (action(dt.date(2026, 5, 12)), "INFY", prices_around(dt.date(2026, 5, 12), -0.5),
         "needs_review"),
        (action(dt.date(2026, 5, 12)), "INFY", None, "needs_review"),  # no prices to check
        (action(None, "buyback", None), "INFY", None, "no_adjustment"),
    ],
)  # fmt: skip
def test_status(act, symbol, prices, status) -> None:
    empty = pd.DataFrame({"date": pd.to_datetime([]), "open": [], "close": []})
    got, note = fc.status_for(act, symbol, YAML, empty if prices is None else prices, TODAY)
    assert got == status
    assert note


# --- merging and the database ------------------------------------------------------


def filing(fid: str, symbol: str, filed: str, category: str, subject: str, desc: str | None):
    return {
        "id": fid,
        "exchange": "BSE" if fid.startswith("b") else "NSE",
        "exchange_id": fid,
        "symbol": symbol,
        "filed_at": pd.Timestamp(filed, tz="UTC").to_pydatetime(),
        "first_seen_at": pd.Timestamp(filed, tz="UTC").to_pydatetime(),
        "category": category,
        "subject": subject,
        "description": desc,
    }


FILINGS = [
    filing("n1", "INFY", "2026-09-01 10:00", "Board Meeting",
           "Board meeting to consider bonus issue", None),
    filing("n2", "INFY", "2026-09-10 12:00", "Outcome of Board Meeting",
           "Outcome of Board Meeting", "Recommended bonus equity shares in the ratio of 1:1."),
    filing("n3", "INFY", "2026-09-20 12:00", "Company Update", "Record Date for Bonus Issue",
           "The Company has fixed Wednesday, October 28, 2026 as the Record Date."),
    filing("b3", "INFY", "2026-09-20 12:01", "Corp. Action", "Record date - bonus issue",
           "Record date is 28th October, 2026 for bonus shares."),
    filing("n4", "TMPV", "2025-09-20 12:00", "Scheme of Arrangement", "Demerger - Record Date",
           "Record date: 14/10/2025 for the demerger."),
    filing("n5", "TCS", "2026-07-09 12:00", "Result", "Financial Results for Q1", None),
]  # fmt: skip


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def test_same_action_across_filings_is_merged() -> None:
    frame = pd.DataFrame(FILINGS)
    rows = fc.detect_actions(frame, YAML, {}, TODAY)
    by_symbol = {r["symbol"]: r for r in rows}
    assert len(rows) == 2  # one INFY bonus (4 filings), one TMPV demerger
    bonus = by_symbol["INFY"]
    assert (bonus["ratio"], bonus["ex_date"], bonus["status"]) == (
        "1:1",
        dt.date(2026, 10, 28),
        "upcoming",
    )
    assert bonus["price_factor"] == 0.5  # ratio from the outcome, date from the record-date filing
    assert "3 filing(s)" in bonus["note"]  # the "to consider" intimation isn't an action
    assert by_symbol["TMPV"]["status"] == "recorded"


def test_run_categorises_stores_and_warns(engine: Engine, caplog) -> None:
    with engine.begin() as conn:
        conn.execute(db.FILINGS.insert(), FILINGS)
    with caplog.at_level(logging.INFO, logger="processing.filing_categories"):
        counts, rows = fc.run(today=TODAY)

    assert counts.to_dict() == {"corporate_action": 4, "board_meeting": 1, "results": 1}
    stored = db.read_filings().set_index("id")
    assert db.read_pending_actions()["symbol"].tolist() == ["INFY", "TMPV"]
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "INFY: bonus 1:1" in warnings[0] and "upcoming" in warnings[0]
    assert not stored.empty


def test_yaml_is_never_modified(engine: Engine) -> None:
    yaml_path = Path(fc.__file__).resolve().parents[1] / "config/corporate_actions.yaml"
    before = yaml_path.read_bytes()
    with engine.begin() as conn:
        conn.execute(db.FILINGS.insert(), FILINGS)
    fc.run(today=TODAY)
    assert yaml_path.read_bytes() == before


def test_no_filings_is_a_clean_no_op(engine: Engine) -> None:
    counts, rows = fc.run(today=TODAY)
    assert counts.empty and rows == []


def test_imported_results_files_stay_classified_as_results() -> None:
    # results.rebuild selects files by filing_type == "results"; classification must not
    # re-label the rows that the results importer/IR collector create.
    for subject in ("FY27Q1 standalone results (xbrl)",
                    "FY23Q1 consolidated+standalone results (pdf) [date approx.]"):  # fmt: skip
        assert fc.classify("Financial Results", subject, None).filing_type == "results"
