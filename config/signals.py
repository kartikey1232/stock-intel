"""Load and validate rule-based signal definitions from config/signals.yaml."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SIGNALS_PATH = Path(__file__).resolve().parent / "signals.yaml"
DIRECTIONS = ("bullish", "bearish", "from_price")

# Required numeric/choice parameters per signal type: name -> allowed values (None = int/float).
_PARAMS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "ma_cross": {"fast": None, "slow": None, "cross": ("above", "below")},
    "rsi_cross": {"length": None, "level": None, "cross": ("above", "below")},
    "volume_spike": {"window": None, "multiple": None},
    "range_breakout": {"window": None, "side": ("high", "low")},
    "gap": {"threshold_pct": None, "side": ("up", "down")},
}
_COMMON = {"type", "direction", "label", "default_on", "cooldown"}
# Optional per-type parameters with defaults. min_gap_pct only affects display and alerts
# (see processing.signals.display_signals), never the stored signals or the backtest.
_OPTIONAL = {"ma_cross": {"min_gap_pct": 0.0}}


class SignalConfigError(ValueError):
    """Raised when the signals file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class SignalRule:
    """One configured signal."""

    name: str
    type: str
    direction: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)
    default_on: bool = False
    cooldown: int = 0


def load_signals(path: Path = DEFAULT_SIGNALS_PATH) -> list[SignalRule]:
    """Load and validate every signal definition, in file order.

    Raises:
        SignalConfigError: on a missing/unparseable file, an unknown type or field, a
            missing or invalid parameter, or a bad direction.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SignalConfigError(f"Signals file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SignalConfigError(f"{path}: invalid YAML: {exc}") from exc
    entries = (raw or {}).get("signals") if isinstance(raw, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise SignalConfigError(f"{path}: expected a non-empty 'signals' mapping")
    return [_parse(name, entry, path) for name, entry in entries.items()]


def _parse(name: str, entry: Any, path: Path) -> SignalRule:
    """Validate one signal definition."""
    where = f"{path}: signals.{name}"
    if not isinstance(entry, dict):
        raise SignalConfigError(f"{where} must be a mapping")
    kind = entry.get("type")
    if kind not in _PARAMS:
        raise SignalConfigError(f"{where}: type must be one of {sorted(_PARAMS)}")
    spec = _PARAMS[kind]
    optional = _OPTIONAL.get(kind, {})
    unknown = sorted(set(entry) - _COMMON - set(spec) - set(optional))
    if unknown:
        raise SignalConfigError(f"{where}: unknown field(s) for {kind}: {', '.join(unknown)}")
    params = {}
    for param, choices in spec.items():
        value = entry.get(param)
        if choices is None:
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise SignalConfigError(f"{where}: {param} must be a positive number")
        elif value not in choices:
            raise SignalConfigError(f"{where}: {param} must be one of {choices}")
        params[param] = value
    for param, default in optional.items():
        value = entry.get(param, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise SignalConfigError(f"{where}: {param} must be a number >= 0")
        params[param] = float(value)
    if kind == "ma_cross" and params["fast"] >= params["slow"]:
        raise SignalConfigError(f"{where}: fast must be shorter than slow")
    if kind == "rsi_cross" and not 0 < params["level"] < 100:
        raise SignalConfigError(f"{where}: level must be between 0 and 100")
    direction = entry.get("direction")
    if direction not in DIRECTIONS:
        raise SignalConfigError(f"{where}: direction must be one of {DIRECTIONS}")
    cooldown = entry.get("cooldown", 0)
    if not isinstance(cooldown, int) or cooldown < 0:
        raise SignalConfigError(f"{where}: cooldown must be a non-negative integer")
    return SignalRule(
        name=name,
        type=kind,
        direction=direction,
        label=str(entry.get("label") or name),
        params=params,
        default_on=entry.get("default_on") is True,
        cooldown=cooldown,
    )
