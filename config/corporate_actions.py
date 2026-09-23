"""Load and validate corporate actions from config/corporate_actions.yaml."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ACTIONS_PATH = Path(__file__).resolve().parent / "corporate_actions.yaml"
ACTION_TYPES = ("split", "bonus", "demerger", "other")
VOLUME_ADJUSTED_TYPES = ("split", "bonus")  # share count changes, so volume scales too

_REQUIRED = ("symbol", "ex_date", "action_type", "price_factor", "source")
_OPTIONAL = ("note",)


class CorporateActionError(ValueError):
    """Raised when the corporate actions file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class CorporateAction:
    """One corporate action affecting price continuity."""

    symbol: str
    ex_date: dt.date
    action_type: str
    price_factor: float
    source: str
    note: str | None = None


def load_corporate_actions(path: Path = DEFAULT_ACTIONS_PATH) -> list[CorporateAction]:
    """Load, validate and return all corporate actions (an empty list is valid).

    Raises:
        CorporateActionError: on a missing/unparseable file, missing or unknown fields,
            a bad action_type, a non-positive price_factor, or a duplicate (symbol, ex_date).
    """
    try:
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise CorporateActionError(f"Corporate actions file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise CorporateActionError(f"{path}: invalid YAML: {exc}") from exc

    entries = raw.get("actions") if isinstance(raw, dict) else None
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise CorporateActionError(f"{path}: 'actions' must be a list")

    actions = [_parse(entry, i, path) for i, entry in enumerate(entries)]
    seen: set[tuple[str, dt.date]] = set()
    for action in actions:
        key = (action.symbol, action.ex_date)
        if key in seen:
            raise CorporateActionError(f"{path}: duplicate action for {key[0]} on {key[1]}")
        seen.add(key)
    return actions


def _parse(entry: Any, index: int, path: Path) -> CorporateAction:
    """Validate one raw entry and convert it to a CorporateAction."""
    where = f"{path}: actions[{index}]"
    if not isinstance(entry, dict):
        raise CorporateActionError(f"{where}: expected a mapping")
    label = f"{where} ({entry.get('symbol', '?')} {entry.get('ex_date', '?')})"

    missing = [f for f in _REQUIRED if f not in entry]
    if missing:
        raise CorporateActionError(f"{label}: missing field(s): {', '.join(missing)}")
    unknown = sorted(set(entry) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise CorporateActionError(f"{label}: unknown field(s): {', '.join(unknown)}")

    if not isinstance(entry["ex_date"], dt.date):
        raise CorporateActionError(f"{label}: 'ex_date' must be a date like 2025-10-14")
    if entry["action_type"] not in ACTION_TYPES:
        raise CorporateActionError(f"{label}: 'action_type' must be one of {ACTION_TYPES}")
    factor = entry["price_factor"]
    if isinstance(factor, bool) or not isinstance(factor, int | float) or factor <= 0:
        raise CorporateActionError(f"{label}: 'price_factor' must be a positive number")
    for name in ("symbol", "source"):
        if not isinstance(entry[name], str) or not entry[name].strip():
            raise CorporateActionError(f"{label}: '{name}' must be a non-empty string")

    note = entry.get("note")
    return CorporateAction(
        symbol=entry["symbol"].strip(),
        ex_date=entry["ex_date"],
        action_type=entry["action_type"],
        price_factor=float(factor),
        source=entry["source"].strip(),
        note=note.strip() if isinstance(note, str) and note.strip() else None,
    )
