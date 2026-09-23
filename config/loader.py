"""Load and validate the stock watchlist from config/watchlist.yaml."""

import datetime as dt
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WATCHLIST_PATH = Path(__file__).resolve().parent / "watchlist.yaml"
VALID_YF_SUFFIXES = (".NS", ".BO")


class WatchlistError(ValueError):
    """Raised when the watchlist file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class ConditionalAlias:
    """Names that only count with specific context nearby, optionally from a date on.

    Before `from_date` the names behave as ordinary aliases (e.g. "Tata Motors" meant the
    combined company before its demerger); on or after it they need one of `context`
    in the same sentence or headline.
    """

    names: tuple[str, ...]
    context: tuple[str, ...]
    from_date: dt.date | None = None


@dataclass(frozen=True)
class Stock:
    """A single watchlist entry."""

    symbol: str
    yf: str
    name: str
    sector: str
    aliases: tuple[str, ...]
    news_terms: tuple[str, ...] = ()
    ambiguous_aliases: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    conditional_aliases: tuple[ConditionalAlias, ...] = ()

    @property
    def search_terms(self) -> tuple[str, ...]:
        """Terms for news search: explicit news_terms, else company name + first alias."""
        if self.news_terms:
            return self.news_terms
        base = re.sub(r"\s+(Ltd\.?|Limited)$", "", self.name, flags=re.IGNORECASE)
        return tuple(dict.fromkeys([base, self.aliases[0]]))


_OPTIONAL_FIELDS = ["news_terms", "ambiguous_aliases", "exclude_patterns", "conditional_aliases"]
_REQUIRED_FIELDS = [f.name for f in fields(Stock) if f.name not in _OPTIONAL_FIELDS]


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
    unknown = sorted(set(entry) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS))
    if unknown:
        raise WatchlistError(f"{where} ({label}): unknown field(s): {', '.join(unknown)}")

    for name in ("symbol", "yf", "name", "sector"):
        value = entry[name]
        if not isinstance(value, str) or not value.strip():
            raise WatchlistError(f"{where} ({label}): '{name}' must be a non-empty string")

    aliases = _string_list(entry, "aliases", f"{where} ({label})", non_empty=True)
    news_terms = _string_list(entry, "news_terms", f"{where} ({label})")
    ambiguous = _string_list(entry, "ambiguous_aliases", f"{where} ({label})")
    excludes = _string_list(entry, "exclude_patterns", f"{where} ({label})")
    conditional = _conditional_aliases(entry.get("conditional_aliases", []), f"{where} ({label})")

    all_names = [*aliases, *ambiguous, *(n for c in conditional for n in c.names)]
    repeated = sorted({n for n in all_names if all_names.count(n) > 1})
    if repeated:
        raise WatchlistError(
            f"{where} ({label}): name(s) listed more than once across aliases, "
            f"ambiguous_aliases and conditional_aliases: {', '.join(repeated)}"
        )

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
        aliases=aliases,
        news_terms=news_terms,
        ambiguous_aliases=ambiguous,
        exclude_patterns=excludes,
        conditional_aliases=conditional,
    )


def _string_list(
    entry: dict[str, Any], name: str, where: str, non_empty: bool = False
) -> tuple[str, ...]:
    """entry[name] as a tuple of stripped strings (missing -> empty), validated."""
    value = entry.get(name, [])
    if (
        not isinstance(value, list)
        or (non_empty and not value)
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        qualifier = "non-empty " if non_empty else ""
        raise WatchlistError(f"{where}: '{name}' must be a {qualifier}list of strings")
    return tuple(v.strip() for v in value)


def _conditional_aliases(raw: Any, where: str) -> tuple[ConditionalAlias, ...]:
    """Validate the conditional_aliases list."""
    if not isinstance(raw, list):
        raise WatchlistError(f"{where}: 'conditional_aliases' must be a list")
    result = []
    for i, item in enumerate(raw):
        at = f"{where}: conditional_aliases[{i}]"
        if not isinstance(item, dict):
            raise WatchlistError(f"{at}: expected a mapping")
        unknown = sorted(set(item) - {"names", "context", "from"})
        if unknown:
            raise WatchlistError(f"{at}: unknown field(s): {', '.join(unknown)}")
        from_date = item.get("from")
        if from_date is not None and not isinstance(from_date, dt.date):
            raise WatchlistError(f"{at}: 'from' must be a date like 2025-10-14")
        result.append(
            ConditionalAlias(
                names=_string_list(item, "names", at, non_empty=True),
                context=_string_list(item, "context", at, non_empty=True),
                from_date=from_date,
            )
        )
    return tuple(result)


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
