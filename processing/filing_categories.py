"""Categorise exchange filings and detect announced corporate actions.

1. Every filing gets one `filing_type` from FILING_TYPES plus `filing_tags` (every type its
   text matched). The exchange's own label stays in `filings.category`. A filing that says
   the board will *consider* something ("Board meeting to consider bonus issue") is a
   board_meeting: nothing has been decided yet.

2. Decided corporate actions (split, bonus, demerger, rights, buyback) have their type,
   ratio, record date and ex-date extracted where the text states them, and are compared
   with config/corporate_actions.yaml. The YAML is never edited: results go to the
   pending_actions table (for review and, later, the dashboard) and to the log.

   Following the YAML policy (only record actions Yahoo did NOT adjust), each action gets
   a status saying what to do:
     recorded       already in the YAML
     upcoming       ex-date not reached: check after it
     undated        no record/ex-date in the text: check manually
     yahoo_adjusted ex-date passed and raw prices show no gap: Yahoo adjusted, add nothing
     needs_review   ex-date passed and raw prices gap (or can't be checked): likely add it
     no_adjustment  buybacks, which never need a price adjustment
   Actions that aren't recorded and may need adding (upcoming, undated, needs_review) are
   logged as warnings; yahoo_adjusted and no_adjustment are logged as info.

   With T+1 settlement (India, since 2023), the ex-date equals the record date, so a
   missing ex-date is taken to be the record date.

Run with:  uv run python -m processing.filing_categories
"""

import datetime as dt
import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd
from dateutil import parser as date_parser

from config.corporate_actions import CorporateAction, load_corporate_actions
from storage.db import (
    init_db,
    read_filings,
    read_prices,
    replace_pending_actions,
    set_filing_types,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
FILING_TYPES = (
    "results", "board_meeting", "dividend", "corporate_action", "shareholding_pattern",
    "press_release", "credit_rating", "insider_trading", "analyst_meet", "other",
)  # fmt: skip
ACTION_TYPES = ("split", "bonus", "demerger", "rights", "buyback")
YAML_MATCH_DAYS = 3  # a YAML ex_date within this many days of the announced one matches
GAP_ADJUSTED = 0.10  # an ex-date overnight move smaller than this means Yahoo adjusted

I = re.IGNORECASE  # noqa: E741


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, I)


# --- classification rules ----------------------------------------------------------

CONSIDER_RE = _rx(
    r"\bto consider\b|\bfor consider(?:ing|ation)\b|\bconsider(?:ing)? and approv|"
    r"\bproposal (?:for|to)\b|\bboard meeting intimation\b|"
    r"\bintimation of (?:the )?board meeting\b|"
    r"\bprior intimation\b|\bmeeting of the board\b[^.]{0,80}\b(?:will|shall|is scheduled)\b"
)
DECIDED_RE = _rx(r"\boutcome of (?:the )?board meeting\b")
ACTION_RES = {
    "bonus": _rx(r"\bbonus (?:issue|shares?|equity shares?)\b"),
    "split": _rx(
        r"\bsub-?division\b|\bstock split\b|\bsplit of (?:the )?(?:equity )?shares\b|"
        r"\bsplit(?:ting)? (?:of )?(?:the )?face value\b"
    ),
    "demerger": _rx(r"\bdemerg(?:er|ed|ing)\b"),
    "rights": _rx(r"\brights issue\b|\brights entitlement\b"),
    "buyback": _rx(r"\bbuy-?back\b"),
}
TYPE_RES = {
    "results": _rx(
        r"\b(?:financial|quarterly|audited|unaudited) results?\b|"
        r"\bintegrated filing[-\s]*financials?\b"
    ),
    "dividend": _rx(r"\bdividend\b(?! distribution policy)"),
    "shareholding_pattern": _rx(r"\bshareholding pattern\b"),
    "credit_rating": _rx(r"\bcredit rating\b|\b(?:CRISIL|ICRA|CARE Ratings|India Ratings)\b"),
    "insider_trading": _rx(
        r"\binsider trading\b|\btrading window\b|\bregulation 7\s*\(2\)|"
        r"\bSEBI \(PIT\)"
    ),
    "analyst_meet": _rx(
        r"\banalysts?\b|\binstitutional investors? meet\b|\bcon\.? ?call\b|"
        r"\b(?:conference|earnings) call\b"
    ),
    "press_release": _rx(r"\b(?:press|media) release\b"),
    "board_meeting": _rx(r"\bboard meeting\b|\bmeeting of the board\b"),
}
# Priority when several types match (corporate actions are checked separately, first).
TYPE_PRIORITY = (
    "results", "dividend", "shareholding_pattern", "credit_rating", "insider_trading",
    "analyst_meet", "press_release", "board_meeting",
)  # fmt: skip


@dataclass(frozen=True)
class Classification:
    """Our type for one filing, every matched type, and the decided actions in it."""

    filing_type: str
    tags: tuple[str, ...]
    actions: tuple[str, ...] = ()


def filing_text(category: str | None, subject: str | None, description: str | None) -> str:
    """The exchange label, subject and description as one whitespace-normalised string.

    Missing parts (None, or NaN from a DataFrame) are skipped.
    """
    parts = (p for p in (category, subject, description) if isinstance(p, str) and p)
    return re.sub(r"\s+", " ", " | ".join(parts))


def classify(category: str | None, subject: str | None, description: str | None) -> Classification:
    """Classify a filing from its exchange category, subject and description."""
    text = filing_text(category, subject, description)
    actions = tuple(a for a, rx in ACTION_RES.items() if rx.search(text))
    types = {t for t, rx in TYPE_RES.items() if rx.search(text)}
    if actions:
        types.add("corporate_action")

    considering = bool(CONSIDER_RE.search(text)) and not DECIDED_RE.search(text)
    if considering:
        # Nothing decided yet: "board meeting to consider bonus issue" is not a bonus.
        tags = tuple(sorted(types | {"board_meeting"}))
        return Classification("board_meeting", tags, ())
    if actions:
        return Classification("corporate_action", tuple(sorted(types)), actions)
    primary = next((t for t in TYPE_PRIORITY if t in types), "other")
    return Classification(primary, tuple(sorted(types)) or ("other",), ())


# --- corporate action extraction ---------------------------------------------------

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}  # fmt: skip
NUM = r"(\d+|" + "|".join(NUMBER_WORDS) + r")"
MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
         r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")  # fmt: skip
DATE = (
    rf"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?"
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?(?:\s+of)?[\s-]+{MONTH}\.?,?[\s-]+\d{{4}}"
    rf"|{MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}[-/.]\d{{1,2}}[-/.]\d{{4}})"
)
RECORD_DATE_RE = _rx(
    rf"\brecord date\b[^.|]{{0,90}}?({DATE})"  # "record date is 14 Oct 2025"
    rf"|({DATE})[^.|]{{0,40}}?\bas (?:the )?record date\b"  # "fixed 14 Oct 2025 as the record date"
)
EX_DATE_RE = _rx(rf"\bex-?date\b[^.|]{{0,60}}?({DATE})")
BONUS_RATIO_RE = _rx(
    rf"\bratio of\s+(\d+)\s*:\s*(\d+)|\b(\d+)\s*:\s*(\d+)\s+bonus\b|"
    rf"\b{NUM}\s*(?:\(\w+\)\s*)?(?:fully paid[- ]up\s+)?bonus (?:equity )?shares?"
    rf"[^.]{{0,40}}?for every\s+{NUM}"
)
FACE_VALUE_RE = _rx(
    r"face value of\s*(?:Rs\.?|₹|INR)\s*(\d+(?:\.\d+)?)[^.|]{0,60}?(?:into|to)\s+[^.|]{0,40}?"
    r"(?:Rs\.?|₹|INR)\s*(\d+(?:\.\d+)?)"
)
SHARES_FOR_EVERY_RE = _rx(
    rf"\b{NUM}\s*(?:\(\w+\)\s*)?(?:fully paid[- ]up\s+)?(?:rights\s+)?(?:equity\s+)?shares?"
    rf"[^.]{{0,80}}?for every\s+{NUM}"
)


def _num(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token.lower()]


def parse_date(text: str) -> dt.date | None:
    """Parse an Indian-style (day-first) date string, or None."""
    try:
        return date_parser.parse(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text), dayfirst=True,
                                 fuzzy=True).date()  # fmt: skip
    except (ValueError, OverflowError):
        return None


@dataclass
class Action:
    """A corporate action extracted from one filing."""

    action_type: str
    ratio: str | None = None
    price_factor: float | None = None
    record_date: dt.date | None = None
    ex_date: dt.date | None = None
    notes: list[str] = field(default_factory=list)


def extract_action(action_type: str, text: str) -> Action:
    """Ratio, price factor and dates for `action_type`, where `text` states them."""
    action = Action(action_type)
    if action_type == "bonus" and (m := BONUS_RATIO_RE.search(text)):
        groups = m.groups()
        pair = next(p for p in (groups[0:2], groups[2:4], groups[4:6]) if p[0])
        new, held = _num(pair[0]), _num(pair[1])
        action.ratio = f"{new}:{held}"  # new shares for every `held` shares
        action.price_factor = held / (new + held)
    elif action_type == "split" and (m := FACE_VALUE_RE.search(text)):
        old, new = float(m.group(1)), float(m.group(2))
        if 0 < new < old:
            action.ratio = f"1:{old / new:g}"
            action.price_factor = new / old
    elif action_type in ("rights", "demerger") and (m := SHARES_FOR_EVERY_RE.search(text)):
        action.ratio = f"{_num(m.group(1))}:{_num(m.group(2))}"
        # Neither has a price factor derivable from the text alone: rights depend on the
        # issue and market price, demergers on the price discovered on the ex-date.

    if m := RECORD_DATE_RE.search(text):
        action.record_date = parse_date(m.group(1) or m.group(2))
    if m := EX_DATE_RE.search(text):
        action.ex_date = parse_date(m.group(1))
    if action.ex_date is None and action.record_date is not None:
        action.ex_date = action.record_date
        action.notes.append("ex-date assumed equal to record date (T+1 settlement)")
    return action


# --- comparison with the YAML and prices -------------------------------------------


def status_for(
    action: Action,
    symbol: str,
    recorded: list[CorporateAction],
    prices: pd.DataFrame,
    today: dt.date,
) -> tuple[str, str]:
    """(status, note) for an announced action; see the module docstring."""
    if action.action_type == "buyback":
        return "no_adjustment", "buybacks don't change the per-share price series"
    if action.ex_date is None:
        return "undated", "no record/ex-date in the filing text"
    for rec in recorded:
        if rec.symbol == symbol and abs((rec.ex_date - action.ex_date).days) <= YAML_MATCH_DAYS:
            return "recorded", f"in corporate_actions.yaml (ex_date {rec.ex_date})"
    if action.ex_date > today:
        return "upcoming", "after the ex-date, check whether Yahoo adjusted it"

    bars = prices[prices["date"].dt.date >= action.ex_date].sort_values("date")
    before = prices[prices["date"].dt.date < action.ex_date].sort_values("date")
    if bars.empty or before.empty:
        return "needs_review", "no stored prices around the ex-date to check"
    gap = bars["open"].iloc[0] / before["close"].iloc[-1] - 1
    note = f"raw overnight move on {bars['date'].iloc[0].date()}: {gap:+.1%}"
    if abs(gap) < GAP_ADJUSTED:
        return "yahoo_adjusted", f"{note}; Yahoo already adjusted, don't add it"
    return "needs_review", f"{note}; unadjusted, add it to corporate_actions.yaml"


def detect_actions(
    filings: pd.DataFrame,
    recorded: list[CorporateAction],
    prices_by_symbol: dict[str, pd.DataFrame],
    today: dt.date,
) -> list[dict]:
    """pending_actions rows for every decided corporate action in `filings`.

    The same action usually appears in several filings (board outcome, record-date
    intimation, both exchanges). Filings are merged per (symbol, type, ex-date); an undated
    mention is folded into a dated one of the same symbol and type within 120 days.
    """
    found: list[tuple[pd.Series, Action]] = []
    for f in filings.itertuples(index=False):
        c = classify(f.category, f.subject, f.description)
        text = filing_text(f.category, f.subject, f.description)
        found += [(f, extract_action(a, text)) for a in c.actions]

    groups: dict[tuple, list[tuple]] = {}
    for f, a in found:
        groups.setdefault((f.symbol, a.action_type, a.ex_date), []).append((f, a))
    for key in [k for k in groups if k[2] is None]:
        symbol, action_type, _ = key
        for other in groups:
            if (
                other[:2] == key[:2]
                and other[2] is not None
                and any(
                    abs((pd.Timestamp(f.filed_at).date() - other[2]).days) <= 120
                    for f, _ in groups[key]
                )
            ):
                groups[other] += groups.pop(key)
                break

    now = dt.datetime.now(dt.UTC)
    rows = []
    for (symbol, action_type, _), members in groups.items():
        members.sort(key=lambda fa: pd.Timestamp(fa[0].filed_at))
        merged = Action(action_type)
        for _, a in members:
            for attr in ("ratio", "price_factor", "record_date", "ex_date"):
                if getattr(merged, attr) is None:
                    setattr(merged, attr, getattr(a, attr))
            merged.notes += [n for n in a.notes if n not in merged.notes]
        prices = prices_by_symbol.get(symbol, pd.DataFrame(columns=["date", "open", "close"]))
        status, note = status_for(merged, symbol, recorded, prices, today)
        first = members[0][0]
        rows.append(
            {
                "id": hashlib.sha256(
                    f"{symbol}|{action_type}|{merged.ex_date or first.id}".encode()
                ).hexdigest()[:32],  # fmt: skip
                "filing_id": first.id,
                "symbol": symbol,
                "action_type": action_type,
                "ratio": merged.ratio,
                "price_factor": merged.price_factor,
                "record_date": merged.record_date,
                "ex_date": merged.ex_date,
                "status": status,
                "note": "; ".join([note, *merged.notes, f"{len(members)} filing(s)"]),
                "filed_at": pd.Timestamp(first.filed_at).to_pydatetime(),
                "subject": first.subject,
                "detected_at": now,
            }
        )
    return rows


def log_actions(rows: list[dict]) -> None:
    """Warn about announced actions that may need adding to the YAML; info for the rest."""
    for r in rows:
        if r["status"] == "recorded":
            continue
        level = (
            logging.WARNING
            if r["status"] in ("upcoming", "undated", "needs_review")
            else logging.INFO
        )
        logger.log(
            level,
            "%s: %s %s (ex-date %s) announced but not in corporate_actions.yaml [%s]: %s",
            r["symbol"], r["action_type"], r["ratio"] or "", r["ex_date"] or "unknown",
            r["status"], r["note"],
        )  # fmt: skip


# --- entry point -------------------------------------------------------------------


def run(today: dt.date | None = None) -> tuple[pd.Series, list[dict]]:
    """Categorise all filings, rebuild pending_actions. Returns (type counts, actions)."""
    filings = read_filings()
    if filings.empty:
        logger.info("No filings stored yet; nothing to categorise")
        replace_pending_actions([])
        return pd.Series(dtype=int), []

    types = {}
    for f in filings.itertuples(index=False):
        c = classify(f.category, f.subject, f.description)
        types[f.id] = (c.filing_type, ",".join(c.tags))
    set_filing_types(types)

    symbols = filings["symbol"].unique()
    rows = detect_actions(
        filings,
        load_corporate_actions(),
        {s: read_prices(s) for s in symbols},
        today or dt.datetime.now(IST).date(),
    )
    replace_pending_actions(rows)
    log_actions(rows)
    counts = pd.Series([t for t, _ in types.values()]).value_counts()
    logger.info("Filing types: %s", counts.to_dict())
    return counts, rows


def main() -> int:
    """Entry point."""
    setup_logging()
    init_db()
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
