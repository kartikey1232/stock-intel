"""Tests for loading config/acknowledged_flags.yaml."""

from pathlib import Path

import pytest

from config.acknowledged_flags import (
    DEFAULT_ACKS_PATH,
    AcknowledgedFlagError,
    load_acknowledged_flags,
)

ENTRY = """
- symbol: TMPV
  quarter: FY26Q2
  basis: consolidated
  metric: net_profit
  flag: 19.4x vs previous quarter (3924)
  reason: "demerger: CV business discontinued, one-time gain"
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "acks.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_entries_are_keyed_by_symbol_quarter_basis_metric(tmp_path: Path) -> None:
    acks = load_acknowledged_flags(write(tmp_path, "acknowledged:" + ENTRY))
    ack = acks[("TMPV", "FY26Q2", "consolidated", "net_profit")]
    assert ack.flag == "19.4x vs previous quarter (3924)"
    assert ack.reason.startswith("demerger")


def test_missing_or_empty_file_acknowledges_nothing(tmp_path: Path) -> None:
    assert load_acknowledged_flags(tmp_path / "absent.yaml") == {}
    assert load_acknowledged_flags(write(tmp_path, "acknowledged: []\n")) == {}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("quarter: FY26Q2", "quarter: 2025-09-30"), "quarter"),
        (("basis: consolidated", "basis: group"), "basis"),
        (("  reason: \"demerger: CV business discontinued, one-time gain\"", "  reason: ''"),
         "empty"),
        (("  flag: 19.4x vs previous quarter (3924)\n", ""), "missing"),
        (("metric: net_profit", "metric: net_profit\n  note: x"), "unknown"),
    ],
)  # fmt: skip
def test_invalid_entries_are_rejected(tmp_path: Path, change, message: str) -> None:
    body = "acknowledged:" + ENTRY.replace(*change)
    with pytest.raises(AcknowledgedFlagError, match=message):
        load_acknowledged_flags(write(tmp_path, body))


def test_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(AcknowledgedFlagError, match="duplicate"):
        load_acknowledged_flags(write(tmp_path, "acknowledged:" + ENTRY + ENTRY))


def test_project_file_is_valid() -> None:
    acks = load_acknowledged_flags(DEFAULT_ACKS_PATH)
    assert acks and all(a.reason for a in acks.values())
