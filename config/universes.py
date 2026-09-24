"""Load price-only research universes from config/universes.yaml."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

from config.loader import DEFAULT_WATCHLIST_PATH, Benchmark, load_benchmarks, load_watchlist

DEFAULT_UNIVERSES_PATH = Path(__file__).resolve().parent / "universes.yaml"


class UniverseError(ValueError):
    """Raised when the universes file is missing or invalid."""


@dataclass(frozen=True)
class Universe:
    """A named list of price-only series, plus demerger ex-dates to exclude events on."""

    name: str
    members: tuple[Benchmark, ...]
    known_demergers: frozenset[tuple[str, dt.date]]


def load_universe(
    name: str,
    path: Path = DEFAULT_UNIVERSES_PATH,
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
) -> Universe:
    """Load one universe.

    Raises:
        UniverseError: if the file or universe is missing, a member is malformed, or a
            member's symbol is duplicated or is also a watchlist stock or benchmark.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise UniverseError(f"{path}: {exc}") from exc
    spec = ((raw or {}).get("universes") or {}).get(name)
    if not isinstance(spec, dict):
        raise UniverseError(f"{path}: no universe named {name!r}")
    members = []
    for i, entry in enumerate(spec.get("members") or []):
        if not isinstance(entry, dict) or set(entry) != {"symbol", "yf", "name"}:
            raise UniverseError(f"{path}: {name}.members[{i}] needs exactly symbol, yf, name")
        if not str(entry["yf"]).endswith((".NS", ".BO")):
            raise UniverseError(f"{path}: {name}.members[{i}] yf must end with .NS or .BO")
        members.append(Benchmark(str(entry["symbol"]), str(entry["yf"]), str(entry["name"])))
    if not members:
        raise UniverseError(f"{path}: universe {name!r} has no members")
    symbols = [m.symbol for m in members]
    taken = {s.symbol for s in load_watchlist(watchlist_path)}
    taken |= {b.symbol for b in load_benchmarks(watchlist_path)}
    clashes = sorted({s for s in symbols if s in taken or symbols.count(s) > 1})
    if clashes:
        raise UniverseError(f"{path}: {name} symbol(s) duplicated or already tracked: {clashes}")
    demergers = set()
    for item in spec.get("known_demergers") or []:
        if not isinstance(item, dict) or not isinstance(item.get("ex_date"), dt.date):
            raise UniverseError(f"{path}: {name}.known_demergers needs symbol and ex_date")
        demergers.add((str(item["symbol"]), item["ex_date"]))
    return Universe(name, tuple(members), frozenset(demergers))
