"""Load corrections to XBRL results figures, from config/results_overrides.yaml.

Loading only checks the file's shape. Whether an override may be applied (a primary
filing contradicts the XBRL, and the XBRL is internally inconsistent) is checked against
the stored filings by processing/results.py `override_problem`.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config.acknowledged_flags import BASES, QUARTER_RE

DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parent / "results_overrides.yaml"
ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
TERM_RE = re.compile(r"\s*([+-]?)\s*([A-Za-z][A-Za-z0-9]*)\s*")

_REQUIRED = ("symbol", "quarter", "basis", "metric", "value", "source", "xbrl_check", "reason")

OverrideKey = tuple[str, str, str, str]  # symbol, fiscal quarter, basis, metric
Terms = list[tuple[int, str]]  # (+1 | -1, XBRL element local name)


class ResultOverrideError(ValueError):
    """Raised when the overrides file is malformed or invalid."""


@dataclass(frozen=True)
class ResultOverride:
    """A corrected value for one XBRL results figure.

    `source` is the accession number of the SEC 6-K whose exhibit prints `value`.
    `xbrl_check` is an identity between XBRL elements of the same filing, such as
    "SegmentProfitBeforeTax = ProfitLossFromOrdinaryActivitiesBeforeTax", that should hold
    but doesn't: the evidence that the XBRL is internally inconsistent.
    """

    symbol: str
    quarter: str
    basis: str
    metric: str
    value: float
    source: str
    xbrl_check: str
    reason: str

    @property
    def key(self) -> OverrideKey:
        """(symbol, quarter, basis, metric), matching a results row."""
        return (self.symbol, self.quarter, self.basis, self.metric)

    def check_sides(self) -> tuple[Terms, Terms]:
        """The two sides of `xbrl_check` as signed element names."""
        left, right = self.xbrl_check.split("=")
        return parse_terms(left), parse_terms(right)


def parse_terms(side: str) -> Terms:
    """ "A - B + C" -> [(1, "A"), (-1, "B"), (1, "C")].

    Raises:
        ResultOverrideError: if the expression isn't a sum of element names.
    """
    terms, pos = [], 0
    while pos < len(side):
        m = TERM_RE.match(side, pos)
        if not m or m.end() == pos or (terms and not m.group(1)):
            raise ResultOverrideError(f"xbrl_check: can't read {side!r}")
        terms.append((-1 if m.group(1) == "-" else 1, m.group(2)))
        pos = m.end()
    if not terms:
        raise ResultOverrideError(f"xbrl_check: empty side in {side!r}")
    return terms


def load_result_overrides(
    path: Path = DEFAULT_OVERRIDES_PATH,
) -> dict[OverrideKey, ResultOverride]:
    """Load and validate overrides, keyed by (symbol, quarter, basis, metric).

    A missing file means no overrides.

    Raises:
        ResultOverrideError: on unparseable YAML, missing or unknown fields, a bad quarter,
            basis, value, accession number or xbrl_check, an empty reason, or a duplicate.
    """
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ResultOverrideError(f"{path}: invalid YAML: {exc}") from exc
    entries = (data or {}).get("overrides") or []
    if not isinstance(entries, list):
        raise ResultOverrideError(f"{path}: 'overrides' must be a list")
    overrides: dict[OverrideKey, ResultOverride] = {}
    for i, entry in enumerate(entries):
        where = f"{path} entry {i + 1}"
        override = _parse_entry(entry, where)
        if override.key in overrides:
            raise ResultOverrideError(f"{where}: duplicate {override.key}")
        overrides[override.key] = override
    return overrides


def _parse_entry(entry: Any, where: str) -> ResultOverride:
    """Validate one YAML entry."""
    if not isinstance(entry, dict):
        raise ResultOverrideError(f"{where}: must be a mapping")
    missing = [f for f in _REQUIRED if f not in entry]
    unknown = sorted(set(entry) - set(_REQUIRED))
    if missing or unknown:
        raise ResultOverrideError(f"{where}: missing {missing}, unknown {unknown}")
    values = {f: str(entry[f]).strip() for f in _REQUIRED if f != "value"}
    if not QUARTER_RE.match(values["quarter"]):
        raise ResultOverrideError(f"{where}: quarter must look like FY26Q2")
    if values["basis"] not in BASES:
        raise ResultOverrideError(f"{where}: basis must be one of {BASES}")
    if not ACCESSION_RE.match(values["source"]):
        raise ResultOverrideError(f"{where}: source must be a 6-K accession number")
    if not values["reason"]:
        raise ResultOverrideError(f"{where}: reason must not be empty")
    if isinstance(entry["value"], bool) or not isinstance(entry["value"], int | float):
        raise ResultOverrideError(f"{where}: value must be a number")
    if values["xbrl_check"].count("=") != 1:
        raise ResultOverrideError(f"{where}: xbrl_check must be 'A [+|- B ...] = C [...]'")
    override = ResultOverride(value=float(entry["value"]), **values)
    try:
        override.check_sides()
    except ResultOverrideError as exc:
        raise ResultOverrideError(f"{where}: {exc}") from None
    return override
