"""Quarterly results: parse stored XBRL/PDF result files into a long-format results table.

Sources, in order of trust:
  xbrl  exchange XBRL (in-capmkt / legacy taxonomies), matched by element local name. Every
        numeric fact for the reporting quarter is stored as `x:<ElementName>`, plus the
        headline metrics below. trust = high.
  pdf   results PDFs, parsed from their text with pdfplumber. Only headline rows are read,
        OCR slips are repaired ("18187 49", "3170830,09", "(581.38"), and trust = low.
        Used only for a (symbol, quarter, basis) that has no XBRL.

Units: monetary values in ₹ crore (XBRL INR / 1e7; PDFs by their "(₹ in lakhs/crore/...)"
header), EPS in ₹ per share, NPA ratios in percent, other XBRL ratios as reported ("pure").

Headline metrics: revenue, total_income, net_profit (attributable to owners where the
filing splits it), eps (basic), and for banks interest_earned, interest_expended, nii
(interest earned - interest expended), gross_npa, net_npa, gross_npa_pct, net_npa_pct.

Validation flags a headline value that moves more than 5x (up or down) from the previous
quarter, or changes sign where that shouldn't happen (revenue, income, NII, NPAs); a
profit or EPS sign change is flagged for checking. Flags usually mean a unit or parsing
error, not a real move.

Run with:  uv run python -m processing.results [--report]
"""

import argparse
import datetime as dt
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pdfplumber
from dateutil import parser as date_parser

from config.loader import Stock, load_watchlist
from storage.db import (
    init_db,
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
    "nii", "gross_npa", "net_npa", "gross_npa_pct", "net_npa_pct",
)  # fmt: skip

# XBRL local names per headline metric, first match wins. The non-bank names are verified
# against a real NSE integrated-filing file; the bank names are unverified until a bank
# (HDFC Bank) XBRL file is imported. Unmatched bank filings are logged.
XBRL_TAGS = {
    "revenue": ["RevenueFromOperations"],
    "total_income": ["Income", "TotalIncome"],
    "net_profit": [
        "ProfitOrLossAttributableToOwnersOfParent",
        "ProfitLossForPeriodAttributableToOwnersOfParent",
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
    "gross_npa": ["GrossNonPerformingAssets", "AmountOfGrossNonPerformingAssets"],
    "net_npa": ["NetNonPerformingAssets", "AmountOfNetNonPerformingAssets"],
    "gross_npa_pct": ["PercentageOfGrossNpa", "PercentageOfGrossNonPerformingAssets"],
    "net_npa_pct": ["PercentageOfNpa", "PercentageOfNetNonPerformingAssets"],
}


class ResultParseError(ValueError):
    """A file isn't a results filing we can read; the message says why."""


@dataclass
class ParsedResult:
    """One company's figures for one quarter and basis from one file."""

    kind: str  # xbrl | pdf
    period_end: dt.date
    basis: str  # standalone | consolidated
    symbol_hint: str | None = None  # NSE symbol stated in the file, if any
    company: str | None = None
    filed_on: dt.date | None = None  # board meeting date, if stated
    values: dict[str, tuple[float, str]] = field(default_factory=dict)  # metric -> (value, unit)


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
        if tag:
            parsed.values[metric] = numeric[tag]
    _add_nii(parsed.values)
    return parsed


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
NUMBER_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?%?")
PDF_ROWS = {  # metric -> label regex; the last matching row in a section wins for net_profit
    "revenue": r"revenue from operations",
    "total_income": r"^\W*\d*\s*total income",
    "interest_earned": r"interest ea(?:r|m)n?ed",
    "interest_expended": r"interest expended",
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
    """Numbers at the end of a results row, in column order. (x) and "(x" are negative."""
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


def parse_file(path: Path, stocks: list[Stock]) -> list[ParsedResult]:
    """Parse a stored results file (XBRL or PDF)."""
    head = path.read_bytes()[:5]
    if head.startswith(b"%PDF"):
        return parse_results_pdf(pdf_pages(path), stocks)
    return [parse_xbrl(path.read_bytes())]


# --- filing metadata ---------------------------------------------------------------

RESULTS_DEADLINE_DAYS = 45  # SEBI LODR: quarterly results within 45 days of quarter end


def filing_date_and_subject(parsed: list[ParsedResult]) -> tuple[dt.date, str]:
    """When a results file was published, and its filings subject line.

    The board-approval date stated in the file; if it doesn't state one, the regulatory
    deadline (quarter end + 45 days), marked "[date approx.]" in the subject.
    """
    first = parsed[0]
    bases = "+".join(sorted({p.basis for p in parsed}))
    subject = f"{fiscal_quarter(first.period_end)} {bases} results ({first.kind})"
    if first.filed_on:
        return first.filed_on, subject
    return first.period_end + dt.timedelta(days=RESULTS_DEADLINE_DAYS), f"{subject} [date approx.]"


# --- building the table --------------------------------------------------------------


def build_rows(parsed: list[tuple[str, str, ParsedResult]], now: dt.datetime) -> list[dict]:
    """Results rows from (symbol, filing_id, ParsedResult) triples, XBRL beating PDF."""
    has_xbrl = {(s, p.period_end, p.basis) for s, _, p in parsed if p.kind == "xbrl"}
    rows: dict[tuple, dict] = {}
    for symbol, filing_id, p in parsed:
        if p.kind == "pdf" and (symbol, p.period_end, p.basis) in has_xbrl:
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
                "trust": "high" if p.kind == "xbrl" else "low",
                "filing_id": filing_id,
                "extracted_at": now,
                "flag": None,
            }
    return list(rows.values())


def changes(results: pd.DataFrame) -> pd.DataFrame:
    """QoQ and YoY changes for headline metrics.

    Columns: symbol, basis, metric, period_end, fiscal_quarter, value, prev_q, qoq, prev_y, yoy.
    Changes are fractional ((v - prev) / |prev|); NaN when the comparison quarter is missing.
    """
    df = results[results["metric"].isin(HEADLINE)].copy()
    if df.empty:
        return df.assign(prev_q=[], qoq=[], prev_y=[], yoy=[])
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
    return df.sort_values([*key, "period_end"]).reset_index(drop=True)


def validate(rows: list[dict]) -> list[dict]:
    """Set `flag` on headline rows with >5x jumps or unexpected sign changes."""
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
    return rows


def rebuild(stocks: list[Stock]) -> list[dict]:
    """Re-extract every stored results file into the results table."""
    files = read_result_files()
    parsed = []
    filing_meta = {}
    for f in files.itertuples(index=False):
        try:
            per_file = parse_file(Path(f.attachment_path), stocks)
        except (ResultParseError, OSError) as exc:
            logger.error("%s: can't parse %s: %s", f.symbol, f.attachment_path, exc)
            continue
        parsed += [(f.symbol, f.id, p) for p in per_file]
        filed_on, subject = filing_date_and_subject(per_file)
        filing_meta[f.id] = (dt.datetime.combine(filed_on, dt.time(), tzinfo=IST), subject)
    # Keep filings.filed_at/subject in step with what the files say (older imports may
    # have been stored before a date could be read).
    update_filing_meta(filing_meta)
    for symbol, _, p in parsed:
        if p.kind == "xbrl" and "x:InterestEarned" not in p.values and symbol == "HDFCBANK":
            logger.warning("HDFCBANK XBRL %s has none of the expected bank tags; check "
                           "XBRL_TAGS against the file", p.period_end)  # fmt: skip
    rows = validate(build_rows(parsed, dt.datetime.now(dt.UTC)))
    replace_results(rows)
    for r in rows:
        if r["flag"]:
            logger.warning("%s %s %s %s: %s", r["symbol"], r["fiscal_quarter"], r["basis"],
                           r["metric"], r["flag"])  # fmt: skip
    logger.info("Results: %d row(s) from %d file(s)", len(rows), len(files))
    return rows


# --- report ------------------------------------------------------------------------


def report(quarters: int = 8) -> pd.DataFrame:
    """Last `quarters` of revenue (or total income for banks) and net profit per stock.

    Consolidated where available, else standalone. Values in ₹ crore; '*' marks
    lower-trust PDF values and '!' flagged ones.
    """
    df = read_results()
    df = df[df["metric"].isin(["revenue", "total_income", "net_profit"])]
    if df.empty:
        return df
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
                marks = ("*" if h["trust"] == "low" else "") + ("!" if h["flag"] else "")
                row[label] = f"{h['value']:,.0f}{marks}"
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
        print("\n* = from PDF (lower trust)   ! = validation flag   values in ₹ crore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
