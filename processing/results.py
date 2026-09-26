"""Quarterly results: parse stored XBRL/PDF result files into a long-format results table.

Sources, in order of trust:
  xbrl  exchange XBRL (in-capmkt / legacy taxonomies), matched by element local name. Every
        numeric fact for the reporting quarter is stored as `x:<ElementName>`, plus the
        headline metrics below. trust = high.
  sec   the Indian results (Ind AS / Indian GAAP, ₹ crore or lakh) exhibits that INFY and
        HDFCBANK furnish to the SEC with a 6-K (collectors/sec_results.py). Headline rows
        of the main results table are read from the HTML, with the precision they're
        printed at. trust = high, but used only for a (symbol, quarter, basis) with no XBRL.
        Where both exist they must agree: see `sec_mismatches`.
  pdf   results PDFs, parsed from their text with pdfplumber. Only headline rows are read,
        OCR slips are repaired ("18187 49", "3170830,09", "(581.38"), and trust = low.
        Used only for a (symbol, quarter, basis) that has neither XBRL nor a 6-K.

Units: monetary values in ₹ crore (XBRL INR / 1e7; PDFs by their "(₹ in lakhs/crore/...)"
header), EPS in ₹ per share, NPA ratios in percent, other XBRL ratios as reported ("pure").

Headline metrics: revenue, total_income, net_profit (attributable to owners where the
filing splits it), eps (basic), and for banks interest_earned, interest_expended, nii
(interest earned - interest expended), provisions (other than tax, and contingencies),
gross_npa, net_npa, gross_npa_pct, net_npa_pct. Bank consolidated XBRL fills the NPA fields
with 0 (they're reported standalone only); those placeholders are dropped.

Comparability: when a quarter reports profit from discontinued operations (e.g. TMPV's
Oct 2025 demerger of the CV business, booked in FY26Q2), its top line excludes that
business while earlier quarters, as originally filed, include it. XBRL has no restated
comparatives, so `changes` marks QoQ/YoY that span such a quarter instead of restating.

Validation flags a headline value that moves more than 5x (up or down) from the previous
quarter, or changes sign where that shouldn't happen (revenue, income, NII, NPAs); a
profit or EPS sign change is flagged for checking. Flags usually mean a unit or parsing
error, not a real move. A flag reviewed in config/acknowledged_flags.yaml (same key and
same flag text) gets `flag_reviewed` set to the reason and logs at INFO, not WARNING.

Run with:  uv run python -m processing.results [--report]
"""

import argparse
import datetime as dt
import logging
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import lxml.html
import pandas as pd
import pdfplumber
from dateutil import parser as date_parser
from lxml import etree

from config.acknowledged_flags import AcknowledgedFlag, FlagKey, load_acknowledged_flags
from config.loader import Stock, load_watchlist
from storage.db import (
    init_db,
    project_path,
    read_result_files,
    read_results,
    replace_results,
    update_filing_meta,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
CRORE = 1e7
JUMP_LIMIT = 5.0
POSITIVE_METRICS = {"revenue", "total_income", "interest_earned", "nii", "gross_npa", "net_npa"}
SIGN_CHECK_METRICS = {"net_profit", "eps"}
HEADLINE = (
    "revenue", "total_income", "net_profit", "eps", "interest_earned", "interest_expended",
    "nii", "provisions", "gross_npa", "net_npa", "gross_npa_pct", "net_npa_pct",
)  # fmt: skip
NPA_METRICS = ("gross_npa", "net_npa", "gross_npa_pct", "net_npa_pct")
DISCONTINUED_METRICS = (
    "x:ProfitLossFromDiscontinuedOperationsAfterTax",
    "x:ProfitLossFromDiscontinuedOperationsBeforeTax",
)

# XBRL local names per headline metric, first match wins. The non-bank names are verified
# against a real NSE integrated-filing file, the bank names against HDFC Bank's FY27Q1
# standalone and consolidated XBRL. Bank filings without the bank tags are logged.
XBRL_TAGS = {
    "revenue": ["RevenueFromOperations"],
    "total_income": ["Income", "TotalIncome"],
    "net_profit": [
        "ProfitOrLossAttributableToOwnersOfParent",
        "ProfitLossForPeriodAttributableToOwnersOfParent",
        # Banks: ProfitLossForThePeriod is before minority interest in consolidated filings.
        "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
        "ProfitLossForPeriod",
        "ProfitLossForThePeriod",
    ],
    "eps": [
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "BasicEarningsLossPerShareFromContinuingOperations",
        "BasicEarningsPerShareBeforeExtraordinaryItems",
    ],
    "interest_earned": ["InterestEarned"],
    "interest_expended": ["InterestExpended"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "gross_npa": ["GrossNonPerformingAssets", "AmountOfGrossNonPerformingAssets"],
    "net_npa": ["NonPerformingAssets", "NetNonPerformingAssets", "AmountOfNetNonPerformingAssets"],
    "gross_npa_pct": ["PercentageOfGrossNpa", "PercentageOfGrossNonPerformingAssets"],
    "net_npa_pct": ["PercentageOfNpa", "PercentageOfNetNonPerformingAssets"],
}


class ResultParseError(ValueError):
    """A file isn't a results filing we can read; the message says why."""


@dataclass
class ParsedResult:
    """One company's figures for one quarter and basis from one file."""

    kind: str  # xbrl | sec | pdf
    period_end: dt.date
    basis: str  # standalone | consolidated
    symbol_hint: str | None = None  # NSE symbol stated in the file, if any
    company: str | None = None
    filed_on: dt.date | None = None  # board meeting date, if stated
    values: dict[str, tuple[float, str]] = field(default_factory=dict)  # metric -> (value, unit)
    # metric -> the printed rounding step, in the value's unit (sec only: 1.0 for whole crore,
    # 0.0001 for lakh with 2 decimals); derived metrics such as nii have none.
    precision: dict[str, float] = field(default_factory=dict)


def fiscal_quarter(period_end: dt.date) -> str:
    """Indian fiscal quarter label: June 2026 -> FY27Q1, March 2026 -> FY26Q4."""
    quarter = {6: 1, 9: 2, 12: 3, 3: 4}.get(period_end.month)
    if quarter is None:
        raise ValueError(f"{period_end} is not a quarter end")
    fiscal_year = period_end.year + (1 if period_end.month >= 4 else 0)
    return f"FY{fiscal_year % 100:02d}Q{quarter}"


def _add_nii(values: dict[str, tuple[float, str]]) -> None:
    """Derive NII from interest earned and expended when both are present."""
    if "nii" not in values and {"interest_earned", "interest_expended"} <= values.keys():
        values["nii"] = (values["interest_earned"][0] - values["interest_expended"][0], "INR crore")


# --- XBRL --------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_day(text: str | None) -> dt.date | None:
    if not text:
        return None
    try:
        return date_parser.parse(text.strip(), dayfirst=False).date()
    except (ValueError, OverflowError):
        return None


def parse_xbrl(content: bytes) -> ParsedResult:
    """Parse an XBRL results instance document.

    Raises:
        ResultParseError: if it isn't XML/XBRL or has no identifiable reporting quarter.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ResultParseError(f"not valid XML: {exc}") from exc
    if _local(root.tag) != "xbrl":
        raise ResultParseError(f"XML but not an XBRL instance (root element <{_local(root.tag)}>)")

    contexts: dict[str, tuple[dt.date | None, dt.date | None, bool]] = {}
    units: dict[str, str] = {}
    facts: list[tuple[str, str, str | None, str]] = []  # name, context, unit, text
    for el in root:
        name = _local(el.tag)
        if name == "context":
            period = next((c for c in el if _local(c.tag) == "period"), None)
            dates = {
                _local(p.tag): _parse_day(p.text) for p in (period if period is not None else [])
            }
            dimensional = any(_local(x.tag) in ("segment", "scenario") for x in el.iter())
            end = dates.get("endDate") or dates.get("instant")
            contexts[el.get("id", "")] = (dates.get("startDate"), end, dimensional)
        elif name == "unit":
            units[el.get("id", "")] = " ".join((m.text or "").strip() for m in el.iter()
                                               if _local(m.tag) == "measure")  # fmt: skip
        elif el.get("contextRef"):
            facts.append((name, el.get("contextRef"), el.get("unitRef"), (el.text or "").strip()))

    _apply_declared_periods(contexts, facts)
    text = {n: t for n, _, u, t in facts if u is None and t}
    start = _parse_day(text.get("DateOfStartOfReportingPeriod"))
    end = _parse_day(text.get("DateOfEndOfReportingPeriod"))
    current = _current_context(contexts, start, end)
    if current is None:
        raise ResultParseError("XBRL has no non-dimensional context for a reporting quarter")
    period_end = contexts[current][1]
    nature = text.get("NatureOfReportStandaloneConsolidated", "")

    parsed = ParsedResult(
        kind="xbrl",
        period_end=period_end,
        basis="consolidated" if "consolidated" in nature.lower() else "standalone",
        symbol_hint=text.get("Symbol"),
        company=text.get("NameOfTheCompany") or text.get("NameOfCompany"),
        filed_on=_parse_day(text.get("DateOfBoardMeetingWhenFinancialResultsWereApproved")),
    )
    numeric: dict[str, tuple[float, str]] = {}
    for name, context, unit_ref, raw in facts:
        if context != current or unit_ref is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        measure = units.get(unit_ref, "")
        if "INR" in measure and "share" in measure.lower():
            numeric[name] = (value, "INR/share")
        elif "INR" in measure:
            numeric[name] = (value / CRORE, "INR crore")
        else:
            numeric[name] = (value, "pure")
    parsed.values = {f"x:{n}": v for n, v in numeric.items()}
    for metric, tags in XBRL_TAGS.items():
        tag = next((t for t in tags if t in numeric), None)
        if tag is None:
            continue
        value, unit = numeric[tag]
        if metric in NPA_METRICS and value == 0:
            continue  # placeholder: banks report NPAs in standalone results only
        if metric.endswith("_pct") and unit == "pure":
            value, unit = value * 100, "%"  # XBRL states 1.17% as 0.0117
        parsed.values[metric] = (value, unit)
    _add_nii(parsed.values)
    return parsed


def _apply_declared_periods(
    contexts: dict[str, tuple[dt.date | None, dt.date | None, bool]],
    facts: list[tuple[str, str, str | None, str]],
) -> None:
    """Replace context dates with the reporting period each context declares, if any.

    Pre-2025 NSE files give the year-to-date context (FourD) the quarter's period dates;
    only its DateOfStartOfReportingPeriod/DateOfEndOfReportingPeriod facts say what it covers.
    """
    fields = {"DateOfStartOfReportingPeriod": 0, "DateOfEndOfReportingPeriod": 1}
    for name, cid, _, raw in facts:
        day = _parse_day(raw) if name in fields else None
        if day and cid in contexts:
            dates = list(contexts[cid])
            dates[fields[name]] = day
            contexts[cid] = (dates[0], dates[1], dates[2])


def _current_context(contexts, start, end) -> str | None:
    """The non-dimensional ~3-month duration context for the reporting quarter.

    Q4 filings also carry full-year contexts; a context counts only if it's quarter-length,
    preferring one that matches the declared reporting dates, then the declared end date.
    """
    plain = {
        cid: (s, e)
        for cid, (s, e, dimensional) in contexts.items()
        if not dimensional and s and e and 80 <= (e - s).days <= 100
    }
    for wanted in ((start, end), (None, end)):
        hits = [cid for cid, (s, e) in plain.items() if e == wanted[1] and wanted[0] in (None, s)]
        if wanted[1] and hits:
            return hits[0]
    return max(plain, key=lambda cid: plain[cid][1], default=None)


# --- PDF ---------------------------------------------------------------------------

MONTHS = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*"
SECTION_RE = re.compile(
    r"(?:UN)?AUDITED\s+(STANDALONE|CONSOLIDATED)\s+(?:FINANCIAL\s+)?RESULTS\s+FOR\s+THE\s+"
    rf"(?:QUARTER|THREE\s+MONTHS)[A-Z\s]*?ENDED\s+({MONTHS}\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{4}})",
    re.IGNORECASE,
)  # fmt: skip
APPROVAL_RE = re.compile(  # "results have been approved by the Board ... held on July 18, 2026"
    r"results[^.]{0,120}?approved by the Board of Directors[^.]{0,120}?held on\s+"
    rf"({MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS},?\s+\d{{4}})",
    re.IGNORECASE,
)
UNIT_RE = re.compile(r"in\s+(crores?|lakhs?|lacs?|thousands?|millions?)\s*\)", re.IGNORECASE)
UNIT_TO_CRORE = {"crore": 1.0, "lakh": 0.01, "lac": 0.01, "thousand": 1e-4, "million": 0.1}
NOTE_REF_RE = re.compile(  # "(Refer note 8)", "(Note 3)", "(refer notes 4 and 5)"
    r"\((?:refer\s+)?notes?\s*\d+(?:\s*(?:,|&|and)\s*\d+)*\s*\)", re.IGNORECASE
)
NUMBER_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?%?")
PDF_ROWS = {  # metric -> label regex; the last matching row in a section wins for net_profit
    "revenue": r"revenue from operations",
    "total_income": r"^\W*\d*\s*total income",
    "interest_earned": r"interest ea(?:r|m)n?ed",
    "interest_expended": r"interest expended",
    "provisions": r"provisions \(other than tax\) and contingencies",
    "net_profit": r"net profit.*for the period(?!.*before minority)",
    "eps": r"\(a\)\s*basic|^\W*basic",
    "gross_npa": r"\(a\)\s*gross npa",
    "net_npa": r"\(b\)\s*net npa",
    "gross_npa_pct": r"% of gross npa",
    "net_npa_pct": r"% of net npa",
}


def repair_ocr_numbers(line: str) -> str:
    """Fix common OCR slips in results tables before reading numbers.

    "18187 49" -> "18187.49" (decimal point lost), "11275,87" -> "11275.87" (decimal read as
    comma; a comma + exactly 2 final digits with no other decimal point).
    """
    line = re.sub(r"(?<![\d.,])(\d{3,})\s(\d{2})(?=\s|$)", r"\1.\2", line)
    return re.sub(r"(?<![\d,])(\d+),(\d{2})(?=\s|$|%)", r"\1.\2", line)


def _numbers(line: str) -> list[float]:
    """Numbers at the end of a results row, in column order. (x) and "(x" are negative.

    Note references in the label ("(Refer note 8)") are removed first, or "8)" would be
    read as the first column.
    """
    line = NOTE_REF_RE.sub(" ", line)
    values = []
    for token in reversed(repair_ocr_numbers(line).split()):
        if not NUMBER_RE.fullmatch(token):
            break
        negative = token.startswith("(")
        clean = token.strip("()%").replace(",", "")
        try:
            values.append(-float(clean) if negative else float(clean))
        except ValueError:
            break
    return list(reversed(values))


def parse_results_pdf(pages: list[str], stocks: list[Stock]) -> list[ParsedResult]:
    """Parse standalone/consolidated quarterly sections from a results PDF's page texts.

    Raises:
        ResultParseError: if no results section or no watchlist company is found.
    """
    text = "\n".join(pages)
    company = identify_company(text[:3000], stocks)
    if company is None:
        raise ResultParseError("no watchlist company named on the first page")
    approval = APPROVAL_RE.search(re.sub(r"\s+", " ", text))
    filed_on = date_parser.parse(approval.group(1)).date() if approval else None
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        raise ResultParseError("no 'standalone/consolidated financial results for the quarter "
                               "ended ...' heading found")  # fmt: skip

    results = []
    for i, m in enumerate(matches):
        section = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        unit = UNIT_RE.search(section[:400])
        if unit is None:
            logger.warning("%s %s section: no '(in crore/lakhs/...)' unit line; skipped",
                           company.symbol, m.group(1))  # fmt: skip
            continue
        factor = UNIT_TO_CRORE[re.sub(r"e?s$", "", unit.group(1).lower())]
        period_end = date_parser.parse(m.group(2), dayfirst=True).date()
        parsed = ParsedResult(
            "pdf", period_end, m.group(1).lower(), company=company.symbol, filed_on=filed_on
        )
        for line in section.splitlines():
            label = line.lower()
            for metric, pattern in PDF_ROWS.items():
                if not re.search(pattern, label):
                    continue
                numbers = _numbers(line)
                if len(numbers) < 2:  # a real row has several period columns
                    continue
                current = numbers[0]
                if metric == "eps":
                    parsed.values.setdefault("eps", (current, "INR/share"))
                elif metric.endswith("_pct"):
                    parsed.values.setdefault(metric, (current, "%"))
                elif metric == "net_profit":
                    parsed.values["net_profit"] = (current * factor, "INR crore")  # last wins
                else:
                    parsed.values.setdefault(metric, (current * factor, "INR crore"))
        _add_nii(parsed.values)
        if parsed.values:
            results.append(parsed)
    if not results:
        raise ResultParseError("results headings found but no readable rows")
    return results


def identify_company(text: str, stocks: list[Stock]) -> Stock | None:
    """The watchlist stock whose name (as 'X Limited'/'X Ltd') appears in `text`."""
    upper = text.upper()
    for stock in stocks:
        base = re.sub(r"\s+(LTD\.?|LIMITED)$", "", stock.name.upper())
        if re.search(rf"\b{re.escape(base)}\s+(?:LIMITED|LTD)\b", upper):
            return stock
    return None


def pdf_pages(path: Path) -> list[str]:
    """Text of each page of a PDF.

    Raises:
        ResultParseError: if it isn't a readable PDF, or has no text layer (a scanned image
            that would need OCR).
    """
    try:
        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # pdfminer raises a variety of syntax errors
        raise ResultParseError(f"not a readable PDF: {exc}") from exc
    if not any(p.strip() for p in pages):
        raise ResultParseError("PDF has no text layer (scanned image); it would need OCR")
    return pages


# --- SEC 6-K exhibits ----------------------------------------------------------------

SEC_DATE = rf"{MONTHS}\.?\s+\d{{1,2}},?\s*\d{{4}}|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{4}}"
SEC_HEADING_RE = re.compile(  # dated section headings, and Infosys's undated standalone one
    r"(standalone|consolidated)\s+(?:(?:un)?audited\s+)?(?:financial\s+)?results\b[^.]{0,120}?"
    rf"for\s+the\s+(?:quarter|three\s+months)[a-z\s-]*?ended\s+({SEC_DATE})"  # "half-year"
    r"|results\s+of\s+[^.()]{0,80}\(\s*(standalone)\b",
    re.IGNORECASE,
)
SEC_APPROVAL_RE = re.compile(  # HDFC: "approved by ...", Infosys: "taken on record by ..."
    r"(?:approved|taken\s+on\s+record)\s+by\s+the\s+Board\s+of\s+Directors[^.]{0,120}?held\s+on\s+"
    rf"({MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS},?\s+\d{{4}})",
    re.IGNORECASE,
)
INDIAN_STANDARDS_RE = re.compile(
    r"\bInd[\s-]?AS\b|Indian\s+Accounting\s+Standards|Indian\s+GAAP", re.IGNORECASE
)
FOREIGN_RE = re.compile(  # IFRS / US GAAP / US dollar sections (Infosys adds an IFRS summary)
    r"\bIFRS\b|International\s+Financial\s+Reporting|US\s*GAAP|US\s*\$|U\.?S\.?\s+dollars",
    re.IGNORECASE,
)
SEC_UNIT_RE = re.compile(r"\bin\s+(crores?|lakhs?|lacs?)\b", re.IGNORECASE)
SEC_NUMBER_RE = re.compile(r"(\()?-?(\d[\d,]*(?:\.(\d+))?)\)?(%)?")
DASHES = {"-", "–", "—", "nil"}
ROW, TABLE = "\ue000", "\ue001"  # private-use line markers: table row, table start
# metric -> label pattern (label lower-cased, whitespace collapsed). The first matching row
# wins, except net_profit (the last "profit for the period" row: HDFC Bank's consolidated
# table lists the before-minority profit first), and owners' profit (Infosys) beats both.
SEC_ROWS = {
    "revenue": r"revenue from operations\b",
    "total_income": r"total income\b",
    "interest_earned": r"interest earned\b",
    "interest_expended": r"interest expended\b",
    "provisions": r"provisions \(other than tax\) and contingencies",
    "net_profit": r"(?:consolidated )?net profit.*for the period(?!.*before minority)"
    r"|profit for the period\b",
    "owners_profit": r"owners of the company\b",
    "eps": r"(?:\(a\)\s*)?basic\b",
    "gross_npa": r"\(a\)\s*gross npas?$",
    "net_npa": r"\(b\)\s*net npas?$",
    "gross_npa_pct": r"(?:\(c\)\s*)?% of gross npas?",
    "net_npa_pct": r"(?:\(d\)\s*)?% of net npas?",
}


def sec_exhibit_text(content: bytes) -> str:
    """Text of an SEC HTML exhibit, with each table row as one line.

    A row line is ROW + its non-empty cells joined by tabs (")" and "%" cells glued to the
    previous cell, lone currency signs dropped); each table starts with a TABLE line.

    Raises:
        ResultParseError: if it isn't parseable HTML.
    """
    try:
        doc = lxml.html.fromstring(content)
    except (etree.ParserError, ValueError) as exc:
        raise ResultParseError(f"not readable HTML: {exc}") from exc
    for el in doc.iter("p", "div", "br", "center"):
        el.tail = "\n" + (el.tail or "")
    for table in list(doc.iter("table")):
        lines = [TABLE]
        for tr in table.iter("tr"):
            cells: list[str] = []
            for td in tr:
                if td.tag not in ("td", "th"):
                    continue
                cell = " ".join(td.text_content().split())
                if cell in (")", "%", ")%") and cells:
                    cells[-1] += cell
                elif cell and cell not in ("₹", "$", "Rs.", "Rs"):
                    cells.append(cell)
            lines.append(ROW + "\t".join(cells))
        table.clear(keep_tail=True)
        table.text = "\n" + "\n".join(lines) + "\n"
    return doc.text_content()


def _sec_row(cells: list[str]) -> tuple[str, list[tuple[float, int] | None]]:
    """(label, values) of a table row; values are (number, decimals) or None for a dash.

    The label is the first cell with a letter (HDFC Bank rows start with a row number).
    A row with any non-numeric cell after the label (a header) has no values.
    """
    idx = next((i for i, c in enumerate(cells) if re.search("[A-Za-z]", c)
                and c.lower() not in DASHES), None)  # fmt: skip
    if idx is None:
        return "", []
    values: list[tuple[float, int] | None] = []
    for cell in cells[idx + 1 :]:
        m = SEC_NUMBER_RE.fullmatch(cell.replace(" ", ""))
        if cell.lower() in DASHES:
            values.append(None)
        elif m:
            number = float(m.group(2).replace(",", ""))
            values.append((-number if m.group(1) else number, len(m.group(3) or "")))
        else:
            return cells[idx], []
    return cells[idx], values


def _sec_metrics(rows: list[list[str]], factor: float) -> tuple[dict, dict]:
    """Headline values and printed rounding steps from one results table's rows."""
    values: dict[str, tuple[float, str]] = {}
    precision: dict[str, float] = {}
    for cells in rows:
        label, numbers = _sec_row(cells)
        if len(numbers) < 2 or numbers[0] is None:  # a real row has several period columns
            continue
        label = " ".join(label.lower().split())
        metric = next((m for m, rx in SEC_ROWS.items() if re.match(rx, label)), None)
        if metric is None or (metric in values and metric != "net_profit"):
            continue
        number, decimals = numbers[0]
        scale, unit = {"eps": (1.0, "INR/share")}.get(metric, (factor, "INR crore"))
        if metric.endswith("_pct"):
            scale, unit = 1.0, "%"
        values[metric] = (number * scale, unit)
        precision[metric] = 10.0**-decimals * scale
    if "owners_profit" in values:
        values["net_profit"] = values.pop("owners_profit")
        precision["net_profit"] = precision.pop("owners_profit")
    return values, precision


PERIOD_RE = re.compile(r"(?:quarter|three\s+months|half[\s-]*year)[a-z\s-]{0,40}ended", re.I)


def has_results_table(content: bytes) -> bool:
    """True if an exhibit mentions a period "ended" and has a table with two or more
    headline rows (different metrics) with figures.

    Used to tell "not a results exhibit" (skip) from "a results exhibit we can't read"
    (fail loudly). It deliberately doesn't need the section heading, so a heading the
    parser misses (Infosys's "quarter and half-year ended") fails loudly instead of
    passing as "no results".
    """
    try:
        text = sec_exhibit_text(content)
    except ResultParseError:
        return False
    if not PERIOD_RE.search(" ".join(text.split())):
        return False
    for table in text.split(TABLE)[1:]:
        metrics = set()
        for line in table.splitlines():
            if line.startswith(ROW):
                label, numbers = _sec_row(line[1:].split("\t"))
                label = " ".join(label.lower().split())
                if len(numbers) >= 2:
                    metrics |= {m for m, rx in SEC_ROWS.items() if re.match(rx, label)}
        if len(metrics) >= 2:
            return True
    return False


def parse_sec_exhibit(content: bytes, stocks: list[Stock]) -> list[ParsedResult]:
    """Parse the Indian standalone/consolidated quarterly results in an SEC 6-K exhibit.

    The exhibit must name a watchlist company, state Ind AS or Indian GAAP, and have
    sections headed "... standalone/consolidated ... results ... for the quarter ended
    <date>" with a ₹ crore/lakh unit line. Sections in US dollars or under IFRS / US GAAP
    are skipped. Only the first table in a section with headline rows is read (later
    ones are segments and notes). An undated standalone heading ("results of Infosys
    Limited (Standalone Information)") takes the period of the heading before it.

    Raises:
        ResultParseError: if any of that is missing, or two sections for the same quarter
            and basis disagree.
    """
    text = sec_exhibit_text(content)
    flat = " ".join(text.replace(ROW, " ").replace(TABLE, " ").split())
    company = identify_company(flat[:5000], stocks)
    if company is None:
        raise ResultParseError("no watchlist company named near the start of the exhibit")
    if not INDIAN_STANDARDS_RE.search(flat):
        raise ResultParseError("no Ind AS / Indian GAAP statement: not the Indian results")
    approval = SEC_APPROVAL_RE.search(flat)
    filed_on = date_parser.parse(approval.group(1)).date() if approval else None

    headings = list(SEC_HEADING_RE.finditer(text))
    found: dict[tuple[dt.date, str], ParsedResult] = {}
    period_end = None
    for i, m in enumerate(headings):
        if m.group(2):
            period_end = date_parser.parse(m.group(2), dayfirst=True).date()
        section = text[m.start() : headings[i + 1].start() if i + 1 < len(headings) else None]
        head = " ".join(section[:800].split())
        unit = SEC_UNIT_RE.search(head)
        if period_end is None or unit is None or FOREIGN_RE.search(head):
            continue
        factor = UNIT_TO_CRORE[re.sub(r"e?s$", "", unit.group(1).lower())]
        for table in section.split(TABLE)[1:]:
            rows = [ln[1:].split("\t") for ln in table.splitlines() if ln.startswith(ROW)]
            values, precision = _sec_metrics(rows, factor)
            if values:
                break
        else:
            continue
        basis = (m.group(1) or m.group(3)).lower()
        parsed = ParsedResult("sec", period_end, basis, company=company.symbol,
                              filed_on=filed_on, values=values, precision=precision)  # fmt: skip
        _add_nii(parsed.values)
        _merge_section(found, parsed)
    if not found:
        raise ResultParseError("no Indian (₹ crore/lakh) standalone or consolidated quarterly "
                               "results table found")  # fmt: skip
    for p in found.values():
        try:
            fiscal_quarter(p.period_end)
        except ValueError as exc:
            raise ResultParseError(f"{exc}; only quarterly results are imported") from exc
    return list(found.values())


def _merge_section(found: dict[tuple[dt.date, str], ParsedResult], new: ParsedResult) -> None:
    """Keep the first section per (quarter, basis); a later one must not contradict it."""
    key = (new.period_end, new.basis)
    old = found.setdefault(key, new)
    if old is new:
        return
    clash = [m for m in old.values.keys() & new.values.keys()
             if abs(old.values[m][0] - new.values[m][0]) > max(old.precision.get(m, 0),
                                                              new.precision.get(m, 0))]  # fmt: skip
    if clash:
        raise ResultParseError(f"two {new.basis} sections for {fiscal_quarter(new.period_end)} "
                               f"disagree on {', '.join(sorted(clash))}")  # fmt: skip


def sec_mismatches(symbol: str, sec: ParsedResult, xbrl: dict[str, float]) -> list[str]:
    """Where a 6-K exhibit's printed figures disagree with XBRL for the same quarter/basis.

    A figure matches when the XBRL value, rounded to the precision the exhibit prints it
    at, equals the printed value (an exact half-step tie may round either way). Only
    metrics printed in the exhibit and present in `xbrl` (metric -> value, same units) are
    compared; derived ones (nii) aren't. Returns one message per mismatch, naming symbol,
    quarter, basis and metric.
    """
    problems = []
    for metric, step in sorted(sec.precision.items()):
        if metric not in xbrl:
            continue
        printed, unit = sec.values[metric]
        tolerance = step / 2 + 1e-9 * max(1.0, abs(printed))
        if abs(xbrl[metric] - printed) > tolerance:
            decimals = max(0, round(-math.log10(step)))
            problems.append(
                f"{symbol} {fiscal_quarter(sec.period_end)} {sec.basis} {metric}: 6-K prints "
                f"{printed:,.{decimals}f} {unit}, XBRL has {xbrl[metric]:,.{decimals + 2}f}"
            )
    return problems


def parse_file(path: Path, stocks: list[Stock]) -> list[ParsedResult]:
    """Parse a stored results file (XBRL, SEC exhibit or PDF)."""
    content = path.read_bytes()
    if content.startswith(b"%PDF"):
        return parse_results_pdf(pdf_pages(path), stocks)
    if is_sec_document(content):
        return parse_sec_exhibit(content, stocks)
    return [parse_xbrl(content)]


def is_sec_document(content: bytes) -> bool:
    """True for an EDGAR document (<DOCUMENT><TYPE>...) or other HTML page."""
    head = content[:1024].lstrip().lower()
    return head.startswith(b"<document>") or b"<html" in head


# --- filing metadata ---------------------------------------------------------------

RESULTS_DEADLINE_DAYS = 45  # SEBI LODR: quarterly results within 45 days of quarter end


def filing_date_and_subject(
    parsed: list[ParsedResult], known: dt.date | None = None
) -> tuple[dt.date, str]:
    """When a results file was published, and its filings subject line.

    The board-approval date stated in the file; else `known` (a date the source itself
    gives, such as a 6-K's EDGAR filing date); else the regulatory deadline (quarter end
    + 45 days), marked "[date approx.]" in the subject.
    """
    first = parsed[0]
    bases = "+".join(sorted({p.basis for p in parsed}))
    subject = f"{fiscal_quarter(first.period_end)} {bases} results ({first.kind})"
    if first.filed_on or known:
        return first.filed_on or known, subject
    return first.period_end + dt.timedelta(days=RESULTS_DEADLINE_DAYS), f"{subject} [date approx.]"


# --- building the table --------------------------------------------------------------


SOURCE_RANK = {"xbrl": 0, "sec": 1, "pdf": 2}  # lower wins for the same quarter and basis


def build_rows(parsed: list[tuple[str, str, ParsedResult]], now: dt.datetime) -> list[dict]:
    """Results rows from (symbol, filing_id, ParsedResult) triples: XBRL > SEC 6-K > PDF."""
    best: dict[tuple, int] = {}
    for s, _, p in parsed:
        key = (s, p.period_end, p.basis)
        best[key] = min(best.get(key, 99), SOURCE_RANK[p.kind])
    rows: dict[tuple, dict] = {}
    for symbol, filing_id, p in parsed:
        if SOURCE_RANK[p.kind] > best[(symbol, p.period_end, p.basis)]:
            continue
        for metric, (value, unit) in p.values.items():
            key = (symbol, p.period_end, p.basis, metric)
            rows[key] = {
                "symbol": symbol,
                "period_end": p.period_end,
                "basis": p.basis,
                "metric": metric,
                "fiscal_quarter": fiscal_quarter(p.period_end),
                "value": value,
                "unit": unit,
                "source": p.kind,
                "trust": "low" if p.kind == "pdf" else "high",
                "filing_id": filing_id,
                "extracted_at": now,
                "flag": None,
                "flag_reviewed": None,
            }
    return list(rows.values())


def cross_check(parsed: list[tuple[str, str, ParsedResult]]) -> list[str]:
    """`sec_mismatches` for every SEC exhibit with XBRL for the same quarter and basis.

    The SEC collector checks this before storing an exhibit; this catches XBRL imported
    after the exhibit was stored.
    """
    xbrl = {(s, p.period_end, p.basis): {m: v for m, (v, _) in p.values.items()}
            for s, _, p in parsed if p.kind == "xbrl"}  # fmt: skip
    return [
        problem
        for s, _, p in parsed
        if p.kind == "sec" and (s, p.period_end, p.basis) in xbrl
        for problem in sec_mismatches(s, p, xbrl[(s, p.period_end, p.basis)])
    ]


def discontinued_quarters(results: pd.DataFrame) -> pd.DataFrame:
    """Quarters reporting non-zero profit from discontinued operations.

    Columns: symbol, basis, period_end (date), fiscal_quarter, amount (₹ crore, after tax
    where stated). These mark breaks in comparability; see `changes`.
    """
    df = results[results["metric"].isin(DISCONTINUED_METRICS) & (results["value"] != 0)]
    df = df.assign(period_end=pd.to_datetime(df["period_end"]).dt.date)
    # prefer the after-tax figure: DISCONTINUED_METRICS is in preference order
    df = df.assign(rank=df["metric"].map(DISCONTINUED_METRICS.index)).sort_values("rank")
    df = df.drop_duplicates(["symbol", "basis", "period_end"])
    return df[["symbol", "basis", "period_end", "fiscal_quarter", "value"]].rename(
        columns={"value": "amount"}
    )


def _months_before(day: dt.date, months: int) -> dt.date:
    """The quarter end `months` before `day`."""
    return (pd.Timestamp(day) - pd.DateOffset(months=months) + pd.offsets.MonthEnd(0)).date()


def _comparability_note(
    breaks: pd.DataFrame, symbol: str, basis: str, prev_end: dt.date, end: dt.date
) -> str:
    """Why a comparison between quarters ending `prev_end` and `end` isn't like-for-like."""
    hits = breaks[
        (breaks["symbol"] == symbol)
        & (breaks["basis"] == basis)
        & (breaks["period_end"] > prev_end)
        & (breaks["period_end"] <= end)
    ]
    return "; ".join(
        f"not like-for-like: {b.fiscal_quarter} reports ₹{b.amount:,.0f} cr from discontinued "
        "operations, and earlier quarters as filed include that business"
        for b in hits.itertuples()
    )


def changes(results: pd.DataFrame) -> pd.DataFrame:
    """QoQ and YoY changes for headline metrics.

    Columns: symbol, basis, metric, period_end, fiscal_quarter, value, prev_q, qoq, prev_y, yoy,
    qoq_note, yoy_note. Changes are fractional ((v - prev) / |prev|); NaN when the
    comparison quarter is missing. A note ("" if none) says when a comparison spans a
    quarter with discontinued operations, so the two quarters cover different businesses.
    """
    breaks = discontinued_quarters(results)
    df = results[results["metric"].isin(HEADLINE)].copy()
    if df.empty:
        return df.assign(prev_q=[], qoq=[], prev_y=[], yoy=[], qoq_note=[], yoy_note=[])
    df["period_end"] = pd.to_datetime(df["period_end"])
    key = ["symbol", "basis", "metric"]
    values = df.set_index([*key, "period_end"])["value"]

    def lookup(row: pd.Series, months: int) -> float:
        target = (row["period_end"] - pd.DateOffset(months=months)) + pd.offsets.MonthEnd(0)
        return values.get((row["symbol"], row["basis"], row["metric"], target), float("nan"))

    df["prev_q"] = df.apply(lookup, axis=1, months=3)
    df["prev_y"] = df.apply(lookup, axis=1, months=12)
    df["qoq"] = (df["value"] - df["prev_q"]) / df["prev_q"].abs()
    df["yoy"] = (df["value"] - df["prev_y"]) / df["prev_y"].abs()
    df["period_end"] = df["period_end"].dt.date
    for col, prev, months in (("qoq_note", "prev_q", 3), ("yoy_note", "prev_y", 12)):
        df[col] = [
            ""
            if pd.isna(getattr(r, prev))
            else _comparability_note(
                breaks, r.symbol, r.basis, _months_before(r.period_end, months), r.period_end
            )
            for r in df.itertuples()
        ]
    return df.sort_values([*key, "period_end"]).reset_index(drop=True)


def validate(
    rows: list[dict], acknowledged: dict[FlagKey, AcknowledgedFlag] | None = None
) -> list[dict]:
    """Set `flag` on headline rows with >5x jumps or unexpected sign changes.

    `flag_reviewed` gets the acknowledgement's reason when `acknowledged` has the row's
    (symbol, quarter, basis, metric) with exactly this flag text; otherwise it's None.
    """
    if not rows:
        return rows
    diff = changes(pd.DataFrame(rows))
    flags: dict[tuple, str] = {}
    for r in diff.itertuples(index=False):
        if pd.isna(r.prev_q):
            continue
        problems = []
        if r.prev_q != 0 and r.value != 0:
            ratio = abs(r.value / r.prev_q)
            if ratio > JUMP_LIMIT or ratio < 1 / JUMP_LIMIT:
                problems.append(f"{ratio:.1f}x vs previous quarter ({r.prev_q:g})")
        if (r.value < 0) != (r.prev_q < 0) and r.value != 0 and r.prev_q != 0:
            if r.metric in POSITIVE_METRICS:
                problems.append("sign change on a metric that should stay positive")
            elif r.metric in SIGN_CHECK_METRICS:
                problems.append("sign change (profit/loss swing?) - check")
        if problems:
            flags[(r.symbol, r.period_end, r.basis, r.metric)] = "; ".join(problems)
    for row in rows:
        row["flag"] = flags.get((row["symbol"], row["period_end"], row["basis"], row["metric"]))
        ack = (acknowledged or {}).get(flag_key(row))
        row["flag_reviewed"] = ack.reason if ack and row["flag"] == ack.flag else None
    return rows


def flag_key(row: dict) -> FlagKey:
    """(symbol, fiscal quarter, basis, metric): how acknowledgements identify a flag."""
    return (row["symbol"], row["fiscal_quarter"], row["basis"], row["metric"])


def log_flags(rows: list[dict], acknowledged: dict[FlagKey, AcknowledgedFlag]) -> None:
    """Log flags: reviewed ones at INFO; new, changed or unreviewed ones at WARNING."""
    for r in rows:
        if not r["flag"]:
            continue
        where = f"{r['symbol']} {r['fiscal_quarter']} {r['basis']} {r['metric']}"
        ack = acknowledged.get(flag_key(r))
        if r["flag_reviewed"]:
            logger.info("%s: %s (reviewed: %s)", where, r["flag"], r["flag_reviewed"])
        elif ack:
            logger.warning("%s: %s (flag changed since it was acknowledged as %r)",
                           where, r["flag"], ack.flag)  # fmt: skip
        else:
            logger.warning("%s: %s", where, r["flag"])
    flagged = {flag_key(r) for r in rows if r["flag"]}
    for key in sorted(set(acknowledged) - flagged):
        logger.info("Acknowledged flag %s no longer occurs; its entry can be removed",
                    " ".join(key))  # fmt: skip


def rebuild(stocks: list[Stock]) -> list[dict]:
    """Re-extract every stored results file into the results table."""
    files = read_result_files()
    parsed = []
    filing_meta = {}
    for f in files.itertuples(index=False):
        try:
            per_file = parse_file(project_path(f.attachment_path), stocks)
        except (ResultParseError, OSError) as exc:
            logger.error("%s: can't parse %s: %s", f.symbol, f.attachment_path, exc)
            continue
        parsed += [(f.symbol, f.id, p) for p in per_file]
        # A 6-K's stored date is its EDGAR filing date when the exhibit states no board date.
        known = pd.Timestamp(f.filed_at).tz_convert(IST).date() if f.exchange == "SEC" else None
        filed_on, subject = filing_date_and_subject(per_file, known)
        filing_meta[f.id] = (dt.datetime.combine(filed_on, dt.time(), tzinfo=IST), subject)
    # Keep filings.filed_at/subject in step with what the files say (older imports may
    # have been stored before a date could be read).
    update_filing_meta(filing_meta)
    for symbol, _, p in parsed:
        if p.kind == "xbrl" and "x:InterestEarned" not in p.values and symbol == "HDFCBANK":
            logger.warning("HDFCBANK XBRL %s has none of the expected bank tags; check "
                           "XBRL_TAGS against the file", p.period_end)  # fmt: skip
    for problem in cross_check(parsed):
        logger.error("SEC 6-K disagrees with XBRL (XBRL used): %s", problem)
    acknowledged = load_acknowledged_flags()
    rows = validate(build_rows(parsed, dt.datetime.now(dt.UTC)), acknowledged)
    replace_results(rows)
    log_flags(rows, acknowledged)
    logger.info("Results: %d row(s) from %d file(s)", len(rows), len(files))
    return rows


# --- report ------------------------------------------------------------------------


def joined_notes(diff: pd.DataFrame) -> str:
    """The distinct QoQ/YoY comparability notes in `diff` rows, labelled by comparison."""
    notes: dict[str, list[str]] = {}
    for r in diff.itertuples():
        for label, note in (("QoQ", r.qoq_note), ("YoY", r.yoy_note)):
            if note and label not in notes.get(note, []):
                notes.setdefault(note, []).append(label)
    return "; ".join(f"{'/'.join(labels)} {note}" for note, labels in notes.items())


def flag_mark(row: pd.Series) -> str:
    """'!' for an unreviewed flag, '~' for a reviewed one, '' for none."""
    if pd.isna(row["flag"]):
        return ""
    return "~" if pd.notna(row.get("flag_reviewed")) else "!"


def report(quarters: int = 8) -> pd.DataFrame:
    """Last `quarters` of revenue (or total income for banks) and net profit per stock.

    Consolidated where available, else standalone. Values in ₹ crore; '*' marks
    lower-trust PDF values, '^' SEC 6-K values, '!' unreviewed flags and '~' reviewed
    ones. `note` says when QoQ/YoY comparisons for that quarter aren't like-for-like (see
    `changes`).
    """
    all_rows = read_results()
    df = all_rows[all_rows["metric"].isin(["revenue", "total_income", "net_profit"])]
    if df.empty:
        return df
    diff = changes(all_rows)
    out = []
    for symbol, g in df.groupby("symbol"):
        basis = "consolidated" if (g["basis"] == "consolidated").any() else "standalone"
        g = g[g["basis"] == basis]
        top_metric = "revenue" if (g["metric"] == "revenue").any() else "total_income"
        for (fq, pe), q in g.groupby(["fiscal_quarter", "period_end"]):
            row = {"symbol": symbol, "basis": basis, "quarter": fq, "period_end": pe}
            for metric, label in ((top_metric, "revenue"), ("net_profit", "net_profit")):
                hit = q[q["metric"] == metric]
                if hit.empty:
                    row[label] = "-"
                    continue
                h = hit.iloc[0]
                marks = {"pdf": "*", "sec": "^"}.get(h["source"], "") + flag_mark(h)
                row[label] = f"{h['value']:,.0f}{marks}"
            d = diff[(diff["symbol"] == symbol) & (diff["basis"] == basis)
                     & (diff["fiscal_quarter"] == fq) & (diff["metric"] == top_metric)]  # fmt: skip
            row["note"] = joined_notes(d)
            out.append(row)
    table = pd.DataFrame(out).sort_values(["symbol", "period_end"])
    return table.groupby("symbol").tail(quarters).reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    """Entry point: rebuild the results table; --report prints the last 8 quarters."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="print last 8 quarters")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    rebuild(load_watchlist())
    if args.report:
        table = report()
        print("No results yet." if table.empty else table.to_string(index=False))
        print("\n* = from PDF (lower trust)   ^ = from SEC 6-K   ! = validation flag   "
              "~ = reviewed flag   values in ₹ crore")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
