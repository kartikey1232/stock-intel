"""Load and validate the stock watchlist from config/watchlist.yaml."""

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WATCHLIST_PATH = Path(__file__).resolve().parent / "watchlist.yaml"
VALID_YF_SUFFIXES = (".NS", ".BO")


class WatchlistError(ValueError):
    """Raised when the watchlist file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class Stock:
    """A single watchlist entry."""

    symbol: str
    yf: str
    name: str
    sector: str
    aliases: tuple[str, ...]


_REQUIRED_FIELDS = [f.name for f in fields(Stock)]


def load_watchlist(path: Path = DEFAULT_WATCHLIST_PATH) -> list[Stock]:
    """Load, validate and return the watchlist.

    Raises:
        WatchlistError: if the file is missing or unparseable, an entry has missing,
            unknown or wrongly typed fields, or a symbol or Yahoo ticker is duplicated.
    """
    raw = _read_yaml(path)
    entries = raw.get("stocks") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise WatchlistError(f"{path}: expected a non-empty 'stocks' list at the top level")

    stocks = [_parse_entry(entry, index, path) for index, entry in enumerate(entries)]
    _check_unique(stocks, "symbol", path)
    _check_unique(stocks, "yf", path)
    return stocks


def _read_yaml(path: Path) -> Any:
    """Read a YAML file, converting I/O and parse failures to WatchlistError."""
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise WatchlistError(f"Watchlist file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise WatchlistError(f"{path}: invalid YAML: {exc}") from exc


def _parse_entry(entry: Any, index: int, path: Path) -> Stock:
    """Validate one raw entry and convert it to a Stock."""
    where = f"{path}: stocks[{index}]"
    if not isinstance(entry, dict):
        raise WatchlistError(f"{where}: expected a mapping, got {type(entry).__name__}")

    label = entry.get("symbol") or f"entry #{index}"
    missing = [name for name in _REQUIRED_FIELDS if name not in entry]
    if missing:
        raise WatchlistError(f"{where} ({label}): missing field(s): {', '.join(missing)}")
    unknown = sorted(set(entry) - set(_REQUIRED_FIELDS))
    if unknown:
        raise WatchlistError(f"{where} ({label}): unknown field(s): {', '.join(unknown)}")

    for name in ("symbol", "yf", "name", "sector"):
        value = entry[name]
        if not isinstance(value, str) or not value.strip():
            raise WatchlistError(f"{where} ({label}): '{name}' must be a non-empty string")

    aliases = entry["aliases"]
    if (
        not isinstance(aliases, list)
        or not aliases
        or not all(isinstance(a, str) and a.strip() for a in aliases)
    ):
        raise WatchlistError(f"{where} ({label}): 'aliases' must be a non-empty list of strings")

    yf_ticker = entry["yf"].strip()
    if not yf_ticker.endswith(VALID_YF_SUFFIXES):
        raise WatchlistError(
            f"{where} ({label}): 'yf' must end with one of {VALID_YF_SUFFIXES}, got {yf_ticker!r}"
        )

    return Stock(
        symbol=entry["symbol"].strip(),
        yf=yf_ticker,
        name=entry["name"].strip(),
        sector=entry["sector"].strip(),
        aliases=tuple(a.strip() for a in aliases),
    )


def _check_unique(stocks: list[Stock], attr: str, path: Path) -> None:
    """Raise WatchlistError if any value of `attr` appears more than once."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for stock in stocks:
        value = getattr(stock, attr).upper()
        if value in seen:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        raise WatchlistError(f"{path}: duplicate {attr}(s): {', '.join(sorted(set(duplicates)))}")
