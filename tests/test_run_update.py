import pytest

import run_update
from collectors.prices import RunSummary

NEWS = ["news collection", "article text", "story grouping", "entity linking", "sentiment"]
FILINGS = ["IR results PDFs", "results inbox import", "filing classification", "results extraction"]
SOCIAL = ["ValuePickr collection", "social linking", "social sentiment", "daily social aggregates"]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(run_update, "init_db", lambda: None)
    monkeypatch.setattr(run_update, "log_summary", lambda summary: None)
    fake_steps(monkeypatch, calls)
    return calls


def fake_steps(
    monkeypatch,
    calls,
    price_failures=None,
    indicator_failures=None,
    changed_actions=None,
    failing_news: dict[str, Exception] | None = None,
    failing_filings: dict[str, Exception] | None = None,
    failing_social: dict[str, Exception] | None = None,
    missing_bars: dict[str, str] | None = None,
) -> None:
    def collect_all(stocks):
        calls.append("prices")
        return RunSummary(rows_written={}, failures=price_failures or {})

    def process_all(symbols, full=False, force_full=None):
        calls.append(f"indicators(full={full}, force_full={sorted(force_full or [])})")
        return indicator_failures or {}

    failing = {**(failing_news or {}), **(failing_filings or {}), **(failing_social or {})}

    def step(name):
        def run():
            calls.append(name)
            if name in failing:
                raise failing[name]

        return run

    monkeypatch.setattr(run_update, "sync_actions_from_config", lambda: changed_actions or set())
    monkeypatch.setattr(run_update, "collect_all", collect_all)
    monkeypatch.setattr(run_update, "process_all", process_all)
    monkeypatch.setattr(run_update, "compute_signals", lambda stocks: calls.append("signals") or {})
    monkeypatch.setattr(run_update, "missing_session_bars", lambda stocks: missing_bars or {})
    monkeypatch.setattr(run_update, "news_steps", lambda stocks: [(n, step(n)) for n in NEWS])
    monkeypatch.setattr(run_update, "filings_steps", lambda stocks: [(n, step(n)) for n in FILINGS])
    monkeypatch.setattr(run_update, "social_steps", lambda stocks: [(n, step(n)) for n in SOCIAL])


def test_runs_prices_then_indicators_then_news_then_filings_then_social(calls) -> None:
    assert run_update.run() == 0
    assert calls == [
        "prices",
        "indicators(full=False, force_full=[])",
        "signals",
        *NEWS,
        *FILINGS,
        *SOCIAL,
    ]


def test_skip_news(calls) -> None:
    assert run_update.run(skip_news=True) == 0
    assert calls == [
        "prices",
        "indicators(full=False, force_full=[])",
        "signals",
        *FILINGS,
        *SOCIAL,
    ]


def test_skip_filings(calls) -> None:
    assert run_update.run(skip_filings=True) == 0
    assert calls == ["prices", "indicators(full=False, force_full=[])", "signals", *NEWS, *SOCIAL]


def test_skip_social(calls) -> None:
    assert run_update.run(skip_social=True) == 0
    assert calls == ["prices", "indicators(full=False, force_full=[])", "signals", *NEWS, *FILINGS]


def test_social_failure_is_isolated_and_named(monkeypatch, calls, tmp_path) -> None:
    fake_steps(monkeypatch, calls, failing_social={"ValuePickr collection": RuntimeError("429")})
    failures_file = tmp_path / "failures.txt"
    assert run_update.run(failures_file=failures_file) == 1
    assert calls[-4:] == SOCIAL  # later social steps still ran
    assert failures_file.read_text().splitlines() == ["social: ValuePickr collection"]


def test_filings_failure_is_isolated_and_sets_exit_code(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, failing_filings={"IR results PDFs": RuntimeError("down")})
    assert run_update.run() == 1
    assert calls[-8:-4] == FILINGS  # later filings steps still ran


def test_indicators_and_news_still_run_after_price_failure(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, price_failures={"INFY": "ConnectionError"})
    assert run_update.run() == 1
    assert calls == [
        "prices",
        "indicators(full=False, force_full=[])",
        "signals",
        *NEWS,
        *FILINGS,
        *SOCIAL,
    ]


def test_missing_bar_is_a_failure(monkeypatch, calls, tmp_path) -> None:
    fake_steps(
        monkeypatch,
        calls,
        price_failures={"INFY": "YFRateLimitError"},
        missing_bars={"INFY": "no bar", "TCS": "no bar"},
    )
    failures_file = tmp_path / "failures.txt"
    assert run_update.run(skip_news=True, failures_file=failures_file) == 1
    # INFY is reported once, as a price failure; its missing bar is a consequence.
    assert failures_file.read_text().splitlines() == ["prices: INFY", "missing bar: TCS"]


def test_failures_file_is_empty_on_success(calls, tmp_path) -> None:
    failures_file = tmp_path / "failures.txt"
    failures_file.write_text("stale\n")
    assert run_update.run(failures_file=failures_file) == 0
    assert failures_file.read_text() == ""


def test_failures_file_names_every_failed_step(monkeypatch, calls, tmp_path) -> None:
    fake_steps(
        monkeypatch,
        calls,
        indicator_failures={"TCS": "KeyError"},
        failing_news={"sentiment": RuntimeError("x")},
        failing_filings={"results extraction": RuntimeError("y")},
    )
    failures_file = tmp_path / "failures.txt"
    assert run_update.run(failures_file=failures_file) == 1
    assert failures_file.read_text().splitlines() == [
        "indicators: TCS",
        "news: sentiment",
        "filings: results extraction",
    ]


def test_signal_failure_is_reported_without_blocking_later_steps(monkeypatch, calls) -> None:
    monkeypatch.setattr(run_update, "compute_signals", lambda stocks: {"TCS": "KeyError"})
    assert run_update.run() == 1
    assert calls[-4:] == SOCIAL


def test_benchmarks_get_prices_and_indicators_but_not_signals(monkeypatch, calls) -> None:
    seen: dict[str, list[str]] = {}

    def collect_all(series):
        seen["prices"] = [s.symbol for s in series]
        return RunSummary(rows_written={}, failures={})

    def process_all(symbols, full=False, force_full=None):
        seen["indicators"] = list(symbols)
        return {}

    def compute_signals(stocks):
        seen["signals"] = [s.symbol for s in stocks]
        return {}

    monkeypatch.setattr(run_update, "collect_all", collect_all)
    monkeypatch.setattr(run_update, "process_all", process_all)
    monkeypatch.setattr(run_update, "compute_signals", compute_signals)
    run_update.run(skip_news=True, skip_filings=True, skip_social=True)
    assert "NIFTY50" in seen["prices"] and "NIFTY50" in seen["indicators"]
    assert "NIFTY50" not in seen["signals"] and "TCS" in seen["signals"]


def test_indicator_failure_sets_exit_code(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, indicator_failures={"TCS": "KeyError"})
    assert run_update.run() == 1


def test_news_failure_sets_exit_code_without_blocking_later_steps(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, failing_news={"article text": RuntimeError("boom")})
    assert run_update.run() == 1
    assert calls[:3] == [
        "prices",
        "indicators(full=False, force_full=[])",
        "signals",
    ]  # prices unaffected
    assert calls[3:] == [*NEWS, *FILINGS, *SOCIAL]  # every later step still ran


def test_partial_feed_failure_counts_as_failure(monkeypatch, calls) -> None:
    error = run_update.StepError("1 source(s) failed: et_markets")
    fake_steps(monkeypatch, calls, failing_news={"news collection": error})
    assert run_update.run() == 1
    assert calls[len(NEWS) + 2] == "sentiment"


def test_broken_news_setup_does_not_stop_prices(monkeypatch, calls) -> None:
    def broken(stocks):
        raise ImportError("no module named transformers")

    monkeypatch.setattr(run_update, "news_steps", broken)
    assert run_update.run() == 1
    assert calls == [
        "prices",
        "indicators(full=False, force_full=[])",
        "signals",
        *FILINGS,
        *SOCIAL,
    ]


def test_full_flag_is_passed_through(calls) -> None:
    run_update.run(full_indicators=True, skip_news=True, skip_filings=True, skip_social=True)
    assert calls[-2] == "indicators(full=True, force_full=[])"


def test_symbols_with_changed_actions_are_fully_recomputed(monkeypatch, calls) -> None:
    fake_steps(monkeypatch, calls, changed_actions={"TMPV"})
    run_update.run(skip_news=True, skip_filings=True, skip_social=True)
    assert calls[-2] == "indicators(full=False, force_full=['TMPV'])"


def test_cli_parses_skip_news(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(run_update, "setup_logging", lambda: None)
    monkeypatch.setattr(run_update, "run", lambda **kw: seen.update(kw) or 0)
    assert run_update.main(["--skip-news"]) == 0
    assert seen == {
        "full_indicators": False,
        "skip_news": True,
        "skip_filings": False,
        "skip_social": False,
        "failures_file": None,
    }
    assert run_update.main(["--skip-filings"]) == 0
    assert seen["skip_filings"] is True
    assert run_update.main(["--skip-social"]) == 0
    assert seen["skip_social"] is True


def test_real_news_steps_are_wired_in_order() -> None:
    names = [name for name, _ in run_update.news_steps([])]
    assert names == [*NEWS, "daily news aggregates"]


def test_real_filings_steps_are_wired_in_order() -> None:
    assert [name for name, _ in run_update.filings_steps([])] == FILINGS


def test_real_social_steps_are_wired_in_order() -> None:
    assert [name for name, _ in run_update.social_steps([])] == SOCIAL
