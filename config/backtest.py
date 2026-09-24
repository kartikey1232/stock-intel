"""Load and validate event-study settings from config/backtest.yaml."""

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_BACKTEST_PATH = Path(__file__).resolve().parent / "backtest.yaml"


class BacktestConfigError(ValueError):
    """Raised when the backtest settings file is missing or invalid."""


@dataclass(frozen=True)
class BacktestConfig:
    """How processing/backtest.py measures signal outcomes."""

    horizons: tuple[int, ...]
    benchmark: str
    cluster_gap: int
    min_events: int
    bootstrap_samples: int
    seed: int


def load_backtest_config(path: Path = DEFAULT_BACKTEST_PATH) -> BacktestConfig:
    """Load and validate the settings.

    Raises:
        BacktestConfigError: on a missing/unparseable file or an invalid value.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BacktestConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BacktestConfigError(f"{path}: expected a mapping")
    horizons = raw.get("horizons")
    if (
        not isinstance(horizons, list)
        or not horizons
        or not all(isinstance(h, int) and h > 0 for h in horizons)
    ):
        raise BacktestConfigError(f"{path}: horizons must be a list of positive integers")
    values = {}
    for key in ("cluster_gap", "min_events", "bootstrap_samples", "seed"):
        value = raw.get(key)
        if not isinstance(value, int) or value < 0:
            raise BacktestConfigError(f"{path}: {key} must be a non-negative integer")
        values[key] = value
    if values["bootstrap_samples"] < 100:
        raise BacktestConfigError(f"{path}: bootstrap_samples must be at least 100")
    benchmark = raw.get("benchmark")
    if not isinstance(benchmark, str) or not benchmark.strip():
        raise BacktestConfigError(f"{path}: benchmark must be a symbol")
    return BacktestConfig(horizons=tuple(sorted(set(horizons))), benchmark=benchmark, **values)
