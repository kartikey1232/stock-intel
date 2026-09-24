from pathlib import Path

import pytest

from config.loader import Benchmark, Stock, WatchlistError, load_benchmarks, load_watchlist

VALID_ENTRY = """
  - symbol: INFY
    yf: INFY.NS
    name: Infosys Ltd
    sector: Information Technology
    aliases: [Infosys]
"""


def write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_real_watchlist_loads() -> None:
    stocks = load_watchlist()
    assert len(stocks) == 5
    assert all(isinstance(s, Stock) for s in stocks)
    assert {s.symbol for s in stocks} == {"RELIANCE", "TCS", "HDFCBANK", "INFY", "TMPV"}
    assert all(s.yf.endswith(".NS") and s.aliases for s in stocks)


def test_valid_entry_is_parsed(tmp_path: Path) -> None:
    [stock] = load_watchlist(write_yaml(tmp_path, "stocks:" + VALID_ENTRY))
    assert stock == Stock(
        symbol="INFY",
        yf="INFY.NS",
        name="Infosys Ltd",
        sector="Information Technology",
        aliases=("Infosys",),
    )


def test_missing_field_raises(tmp_path: Path) -> None:
    body = "stocks:\n  - symbol: INFY\n    yf: INFY.NS\n    name: Infosys Ltd\n"
    with pytest.raises(WatchlistError, match=r"INFY.*missing field\(s\): sector, aliases"):
        load_watchlist(write_yaml(tmp_path, body))


def test_duplicate_symbol_raises(tmp_path: Path) -> None:
    body = "stocks:" + VALID_ENTRY + VALID_ENTRY.replace("INFY.NS", "INFY.BO")
    with pytest.raises(WatchlistError, match="duplicate symbol"):
        load_watchlist(write_yaml(tmp_path, body))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("stocks: []", "non-empty 'stocks' list"),
        ("stocks:" + VALID_ENTRY.replace("INFY.NS", "INFY"), "'yf' must end with"),
        ("stocks:" + VALID_ENTRY.replace("[Infosys]", "[]"), "'aliases' must be"),
        ("stocks:" + VALID_ENTRY + "    exchange: NSE\n", "unknown field"),
        ("stocks: [unclosed", "invalid YAML"),
    ],
)
def test_invalid_configs_raise(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(WatchlistError, match=message):
        load_watchlist(write_yaml(tmp_path, body))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(WatchlistError, match="not found"):
        load_watchlist(tmp_path / "nope.yaml")


def test_benchmarks_are_loaded_separately_from_stocks(tmp_path: Path) -> None:
    body = (
        "stocks:"
        + VALID_ENTRY
        + '\nbenchmarks:\n  - {symbol: NIFTY50, yf: "^NSEI", name: Nifty 50}\n'
    )
    path = write_yaml(tmp_path, body)
    assert load_benchmarks(path) == [Benchmark("NIFTY50", "^NSEI", "Nifty 50")]
    assert [s.symbol for s in load_watchlist(path)] == ["INFY"]
    assert load_benchmarks(write_yaml(tmp_path, "stocks:" + VALID_ENTRY)) == []


@pytest.mark.parametrize(
    ("benchmark", "message"),
    [
        ('{symbol: INFY, yf: "^NSEI", name: Clash}', "clash"),
        ("{symbol: NIFTY50, yf: NSEI, name: Nifty 50}", "index"),
        ('{symbol: NIFTY50, yf: "^NSEI"}', "exactly"),
    ],
)
def test_invalid_benchmarks_raise(tmp_path: Path, benchmark: str, message: str) -> None:
    path = write_yaml(tmp_path, "stocks:" + VALID_ENTRY + f"\nbenchmarks:\n  - {benchmark}\n")
    with pytest.raises(WatchlistError, match=message):
        load_benchmarks(path)


def test_project_benchmark_is_nifty_50() -> None:
    assert [(b.symbol, b.yf) for b in load_benchmarks()] == [("NIFTY50", "^NSEI")]
