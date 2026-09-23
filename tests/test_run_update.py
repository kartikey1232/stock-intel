import pytest

import run_update
from collectors.prices import RunSummary


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(run_update, "init_db", lambda: None)
    monkeypatch.setattr(run_update, "log_summary", lambda summary: None)
    return calls


def fake_steps(
    monkeypatch, calls, price_failures=None, indicator_failures=None, changed_actions=None
) -> None:
    def collect_all(stocks):
        calls.append("prices")
        return RunSummary(rows_written={}, failures=price_failures or {})

    def process_all(symbols, full=False, force_full=None):
        calls.append(f"indicators(full={full}, force_full={sorted(force_full or [])})")
        return indicator_failures or {}

    monkeypatch.setattr(run_update, "sync_actions_from_config", lambda: changed_actions or set())

    monkeypatch.setattr(run_update, "collect_all", collect_all)
    monkeypatch.setattr(run_update, "process_all", process_all)


def test_runs_prices_then_indicators(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls)
    assert run_update.run() == 0
    assert calls == ["prices", "indicators(full=False, force_full=[])"]


def test_indicators_still_run_after_price_failure(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, price_failures={"INFY": "ConnectionError"})
    assert run_update.run() == 1
    assert calls == ["prices", "indicators(full=False, force_full=[])"]


def test_indicator_failure_sets_exit_code(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, indicator_failures={"TCS": "KeyError"})
    assert run_update.run() == 1


def test_full_flag_is_passed_through(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls)
    run_update.run(full_indicators=True)
    assert calls[-1] == "indicators(full=True, force_full=[])"


def test_symbols_with_changed_actions_are_fully_recomputed(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, changed_actions={"TMPV"})
    run_update.run()
    assert calls[-1] == "indicators(full=False, force_full=['TMPV'])"
