"""Load results validation flags the user has reviewed, from config/acknowledged_flags.yaml."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ACKS_PATH = Path(__file__).resolve().parent / "acknowledged_flags.yaml"
BASES = ("standalone", "consolidated")
QUARTER_RE = re.compile(r"^FY\d{2}Q[1-4]$")

_REQUIRED = ("symbol", "quarter", "basis", "metric", "flag", "reason")

FlagKey = tuple[str, str, str, str]  # symbol, fiscal quarter, basis, metric


class AcknowledgedFlagError(ValueError):
    """Raised when the acknowledged flags file is malformed or invalid."""


@dataclass(frozen=True)
class AcknowledgedFlag:
    """One reviewed validation flag.

    `flag` is the exact flag text that was reviewed: if validation later produces
    different text for the same key (new data, a parser change), the flag warns again.
    """

    symbol: str
    quarter: str
    basis: str
    metric: str
    flag: str
    reason: str

    @property
    def key(self) -> FlagKey:
        """(symbol, quarter, basis, metric), matching a results row."""
        return (self.symbol, self.quarter, self.basis, self.metric)


def load_acknowledged_flags(path: Path = DEFAULT_ACKS_PATH) -> dict[FlagKey, AcknowledgedFlag]:
    """Load and validate acknowledged flags, keyed by (symbol, quarter, basis, metric).

    A missing file means nothing is acknowledged.

    Raises:
        AcknowledgedFlagError: on unparseable YAML, missing or unknown fields, a bad
            quarter label or basis, an empty reason, or a duplicate key.
    """
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AcknowledgedFlagError(f"{path}: invalid YAML: {exc}") from exc
    entries = (data or {}).get("acknowledged") or []
    if not isinstance(entries, list):
        raise AcknowledgedFlagError(f"{path}: 'acknowledged' must be a list")
    acks: dict[FlagKey, AcknowledgedFlag] = {}
    for i, entry in enumerate(entries):
        ack = _parse_entry(entry, f"{path} entry {i + 1}")
        if ack.key in acks:
            raise AcknowledgedFlagError(f"{path} entry {i + 1}: duplicate {ack.key}")
        acks[ack.key] = ack
    return acks


def _parse_entry(entry: Any, where: str) -> AcknowledgedFlag:
    """Validate one YAML entry."""
    if not isinstance(entry, dict):
        raise AcknowledgedFlagError(f"{where}: must be a mapping")
    missing = [f for f in _REQUIRED if f not in entry]
    unknown = sorted(set(entry) - set(_REQUIRED))
    if missing or unknown:
        raise AcknowledgedFlagError(f"{where}: missing {missing}, unknown {unknown}")
    values = {f: str(entry[f]).strip() for f in _REQUIRED}
    if not QUARTER_RE.match(values["quarter"]):
        raise AcknowledgedFlagError(f"{where}: quarter must look like FY26Q2")
    if values["basis"] not in BASES:
        raise AcknowledgedFlagError(f"{where}: basis must be one of {BASES}")
    if not values["reason"] or not values["flag"]:
        raise AcknowledgedFlagError(f"{where}: flag and reason must not be empty")
    return AcknowledgedFlag(**values)
