"""Load and validate alert settings from config/alerts.yaml."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ALERTS_PATH = Path(__file__).resolve().parent / "alerts.yaml"
ALERT_TYPES = ("results", "price_move", "price_signal", "news_shift", "pending_action", "pipeline")
SEVERITIES = ("high", "normal")


class AlertConfigError(ValueError):
    """Raised when the alerts file is missing or invalid."""


@dataclass(frozen=True)
class AlertRule:
    """One alert type's settings; `params` holds its type-specific values."""

    type: str
    enabled: bool
    severity: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutOfSample:
    """A signal's registered out-of-sample result: the horizons tested and the verdict."""

    horizons: tuple[int, ...]
    status: str


@dataclass(frozen=True)
class AlertsConfig:
    """All alert rules plus the backtest-note settings."""

    rules: dict[str, AlertRule]
    note_horizon: int
    out_of_sample: dict[str, OutOfSample]

    def note_horizons(self, signal: str) -> tuple[int, ...]:
        """Horizons a signal's backtest note quotes: the out-of-sample ones if tested."""
        oos = self.out_of_sample.get(signal)
        return oos.horizons if oos else (self.note_horizon,)

    def rule(self, alert_type: str) -> AlertRule | None:
        """The rule for `alert_type` if it's enabled."""
        rule = self.rules.get(alert_type)
        return rule if rule and rule.enabled else None


def load_alerts_config(path: Path = DEFAULT_ALERTS_PATH) -> AlertsConfig:
    """Load and validate the alerts file.

    Raises:
        AlertConfigError: on a missing/unparseable file, an unknown alert type, or a bad
            severity or enabled flag.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AlertConfigError(f"{path}: {exc}") from exc
    entries = (raw or {}).get("alerts")
    if not isinstance(entries, dict):
        raise AlertConfigError(f"{path}: expected an 'alerts' mapping")
    rules = {}
    for name, entry in entries.items():
        if name not in ALERT_TYPES:
            raise AlertConfigError(f"{path}: unknown alert type {name!r}")
        if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            raise AlertConfigError(f"{path}: alerts.{name}.enabled must be true or false")
        if entry.get("severity") not in SEVERITIES:
            raise AlertConfigError(f"{path}: alerts.{name}.severity must be one of {SEVERITIES}")
        params = {k: v for k, v in entry.items() if k not in ("enabled", "severity")}
        rules[name] = AlertRule(name, entry["enabled"], entry["severity"], params)
    note = (raw or {}).get("backtest_note") or {}
    horizon = note.get("horizon", 20)
    if not isinstance(horizon, int) or horizon <= 0:
        raise AlertConfigError(f"{path}: backtest_note.horizon must be a positive integer")
    out_of_sample = {}
    for signal, spec in (note.get("out_of_sample") or {}).items():
        horizons = spec.get("horizons") if isinstance(spec, dict) else None
        if (
            not isinstance(horizons, list)
            or not horizons
            or not all(isinstance(h, int) and h > 0 for h in horizons)
            or not str(spec.get("status") or "").strip()
        ):
            raise AlertConfigError(
                f"{path}: backtest_note.out_of_sample.{signal} needs horizons and a status"
            )
        out_of_sample[str(signal)] = OutOfSample(tuple(horizons), str(spec["status"]))
    return AlertsConfig(rules, horizon, out_of_sample)
