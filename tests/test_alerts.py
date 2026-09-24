"""Tests for the alert engine and digest (synthetic data; no network, tmp database only)."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.alerts as al
from config.alerts import load_alerts_config
from config.loader import Stock
from config.signals import load_signals
from processing.backtest import TOO_FEW
from processing.sentiment import TradingCalendar
from storage import db

INFY = Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys",))
DAYS = list(pd.bdate_range("2026-08-03", "2026-09-24"))
DAY = dt.date(2026, 9, 24)
UTC = dt.UTC


def bars(last_close: float = 100.0, last_open: float = 100.0) -> pd.DataFrame:
    close = [100.0] * (len(DAYS) - 1) + [last_close]
    opens = [100.0] * (len(DAYS) - 1) + [last_open]
    return pd.DataFrame(
        {"date": DAYS, "open": opens, "high": close, "low": close, "close": close, "volume": 1000}
    )


def backtest(**notes: tuple[int, str]) -> pd.DataFrame:
    rows = [{"signal": s, "horizon": 20, "n": n, "note": note} for s, (n, note) in notes.items()]
    return pd.DataFrame(rows)


def context(**overrides) -> al.Context:
    ctx = al.Context([INFY], load_alerts_config(), load_signals(), "fake/model")
    ctx.prices = {"INFY": bars()}
    ctx.signals = {"INFY": pd.DataFrame(columns=["date", "signal", "direction", "value"])}
    ctx.news_daily = pd.DataFrame(
        columns=["symbol", "session_date", "story_count", "weighted_score"]
    )
    ctx.news = {"INFY": pd.DataFrame()}
    ctx.filings = pd.DataFrame(
        columns=["id", "symbol", "filing_type", "filed_at", "first_seen_at", "subject"]
    )
    ctx.results = pd.DataFrame()
    ctx.pending = pd.DataFrame()
    ctx.runs = pd.DataFrame(columns=["started_at", "finished_at", "exit_code", "failures"])
    ctx.backtest = backtest(
        gap_down=(40, "no clear difference"),
        golden_cross=(15, TOO_FEW),
        high_52w_breakout=(29, TOO_FEW),
    )
    ctx.calendar = TradingCalendar([d.date() for d in DAYS])
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def signal_rows(*rows: tuple[str, str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"date": pd.Timestamp(DAY), "signal": s, "direction": d, "value": v} for s, d, v in rows]
    )


# --- price alerts ------------------------------------------------------------------


def test_big_move_with_gap_carries_news_filing_and_a_backtest_note() -> None:
    filings = pd.DataFrame(
        [
            {
                "id": "f1",
                "symbol": "INFY",
                "filing_type": "board_meeting",
                "filed_at": pd.Timestamp("2026-09-23 18:30", tz=UTC),
                "first_seen_at": pd.Timestamp("2026-09-24 05:00", tz=UTC),
                "subject": "Board meeting on 24 Sep",
            }
        ]
    )
    news = pd.DataFrame(
        [{"symbol": "INFY", "session_date": DAY, "story_count": 2, "weighted_score": -0.41}]
    )
    ctx = context(
        prices={"INFY": bars(95.5, 96.0)},
        filings=filings,
        news_daily=news,
        signals={"INFY": signal_rows(("gap_down", "bearish", -4.0))},
    )
    [a] = al.price_move_alerts(ctx, INFY, DAY)
    assert a["text"].startswith("INFY fell 4.5% on 24 Sep (close ₹95.50).")
    assert "opened 4.0% below the previous close" in a["text"]
    assert "News that session: 2 stories, sentiment -0.41." in a["text"]
    assert "Filing that day: Board meeting on 24 Sep." in a["text"]
    assert "historically no edge vs doing nothing at 20 trading days (n=40)" in a["text"]
    assert "not confirmed out of sample (docs/hypotheses.md H1)" in a["text"]


def test_small_move_without_gap_is_quiet_and_plain_moves_say_untested() -> None:
    assert al.price_move_alerts(context(prices={"INFY": bars(102.0)}), INFY, DAY) == []
    [a] = al.price_move_alerts(context(prices={"INFY": bars(104.0)}), INFY, DAY)
    assert "rose 4.0%" in a["text"] and "isn't a tested signal" in a["text"]


def test_price_signals_get_a_note_and_too_few_events_say_so() -> None:
    ctx = context(
        signals={
            "INFY": signal_rows(
                ("high_52w_breakout", "bullish", 1.2), ("rsi_below_30", "bullish", 28.0)
            )
        }
    )
    [a] = al.price_signal_alerts(ctx, INFY, DAY)  # RSI isn't a configured alert signal
    assert "52-week high breakout on 24 Sep, +1.20% beyond" in a["text"]
    assert "too few past events to judge (n=29)" in a["text"]


# --- news shift --------------------------------------------------------------------


def news_setup(scores_7d: float) -> al.Context:
    daily = []
    for d in DAYS:
        recent = d.date() > DAY - dt.timedelta(days=7)
        daily.append(
            {
                "symbol": "INFY",
                "session_date": d.date(),
                "story_count": 1,
                "weighted_score": scores_7d if recent else 0.0,
            }
        )
    titles = [
        "Infosys wins large deal",
        "Buy Infosys, target ₹2,000: broker",
        "Infosys raises guidance",
        "Infosys margins improve",
        "Stocks to watch: Infosys",
    ]
    news = pd.DataFrame(
        [
            {
                "article_id": f"a{i}",
                "story_id": None,
                "title": t,
                "source": "Mint",
                "published_at": pd.Timestamp(DAY - dt.timedelta(days=i % 3), tz=UTC)
                + pd.Timedelta(hours=4),
                "first_seen_at": pd.Timestamp(DAY, tz=UTC) + pd.Timedelta(hours=5),
                "score": 0.9 - 0.1 * i,
            }
            for i, t in enumerate(titles)
        ]
    )
    return context(news_daily=pd.DataFrame(daily), news={"INFY": news})


def test_news_shift_alerts_once_with_top_stories_but_no_tips() -> None:
    ctx = news_setup(0.6)
    built = [a for d in DAYS[-10:] for a in al.news_shift_alerts(ctx, INFY, d.date())]
    [a] = built  # only on the first day of the shift, not every day it lasts
    assert "more positive" in a["text"] and "Top stories:" in a["text"]
    assert "Buy Infosys" not in a["text"] and "Stocks to watch" not in a["text"]


def test_small_shift_is_quiet() -> None:
    ctx = news_setup(0.1)
    assert all(al.news_shift_alerts(ctx, INFY, d.date()) == [] for d in DAYS[-7:])


# --- results, pending, pipeline ----------------------------------------------------


def results_setup(board: str) -> al.Context:
    filings = pd.DataFrame(
        [
            {
                "id": "r1",
                "symbol": "INFY",
                "filing_type": "results",
                "filed_at": pd.Timestamp(board, tz=UTC),
                "first_seen_at": pd.Timestamp("2026-09-24 05:00", tz=UTC),
                "subject": "FY27Q2 consolidated results (xbrl)",
            }
        ]
    )
    rows = []
    for end, rev, profit in (
        ("2025-09-30", 44490, 7364),
        ("2026-06-30", 48211, 7769),
        ("2026-09-30", 50000, 8000),
    ):
        for metric, value in (("revenue", rev), ("net_profit", profit)):
            rows.append(
                {
                    "symbol": "INFY",
                    "period_end": dt.date.fromisoformat(end),
                    "basis": "consolidated",
                    "metric": metric,
                    "value": value,
                    "fiscal_quarter": {
                        "2025-09-30": "FY26Q2",
                        "2026-06-30": "FY27Q1",
                        "2026-09-30": "FY27Q2",
                    }[end],
                    "filing_id": "r1" if end == "2026-09-30" else "old",
                    "trust": "high",
                    "flag": None,
                }
            )
    return context(filings=filings, results=pd.DataFrame(rows))


def test_new_results_alert_with_yoy_and_qoq() -> None:
    [a] = al.results_alerts(results_setup("2026-09-22"), INFY, DAY)
    assert a["severity"] == "high" and a["subject"] == "r1"
    assert "FY27Q2 consolidated results imported" in a["text"]
    assert "revenue ₹50,000 cr (YoY +12.4%, QoQ +3.7%)" in a["text"]


def test_backfilled_old_results_never_alert() -> None:
    assert al.results_alerts(results_setup("2026-07-20"), INFY, DAY) == []


def test_pending_actions_and_pipeline_failures() -> None:
    pending = pd.DataFrame(
        [
            {
                "id": "p1",
                "symbol": "INFY",
                "action_type": "bonus",
                "ratio": "1:1",
                "ex_date": dt.date(2026, 10, 5),
                "status": "upcoming",
            },
            {
                "id": "p2",
                "symbol": "INFY",
                "action_type": "split",
                "ratio": None,
                "ex_date": None,
                "status": "yahoo_adjusted",
            },
        ]
    )
    runs = pd.DataFrame(
        [
            {
                "started_at": pd.Timestamp("2026-09-24 10:45", tz=UTC),
                "finished_at": pd.Timestamp("2026-09-24 10:50", tz=UTC),
                "exit_code": 1,
                "failures": "news: sentiment\nprices: TCS\n",
            }
        ]
    )
    ctx = context(pending=pending, runs=runs)
    [p] = al.pending_alerts(ctx, INFY, DAY)
    assert "bonus 1:1 detected" in p["text"] and "status upcoming" in p["text"]
    [f] = al.pipeline_alerts(ctx, DAY)
    assert f["symbol"] == "*" and "16:15 IST failed: news: sentiment, prices: TCS." in f["text"]
    built = al.build_alerts(ctx, DAY, latest=False)  # pending only for the latest day
    assert [a["subject"] for a in built] == [f["subject"]]


# --- rules, storage, digest --------------------------------------------------------


def test_advice_like_text_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        al,
        "price_move_alerts",
        lambda ctx, s, d: [
            al.alert(ctx, "INFY", "price_move", "x", d, "INFY rose 5%: time to buy.")
        ],
    )
    with pytest.raises(ValueError, match="advice"):
        al.build_alerts(context(), DAY, latest=True)


@pytest.mark.parametrize("word", ["buy", "Sell", "accumulate", "target price", "recommended"])
def test_forbidden_words(word: str) -> None:
    assert al.FORBIDDEN_RE.search(f"text with {word} in it")
    assert not al.FORBIDDEN_RE.search("historically no edge vs doing nothing")


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def test_rerunning_a_day_never_duplicates_alerts(engine: Engine) -> None:
    ctx = context(prices={"INFY": bars(104.0)})
    rows = al.build_alerts(ctx, DAY, latest=True)
    assert db.insert_new_alerts(rows) == 1
    assert db.insert_new_alerts(al.build_alerts(ctx, DAY, latest=True)) == 0
    stored = db.read_alerts(DAY, DAY)
    assert len(stored) == 1 and stored["sent_at"].isna().all()


def test_digest_has_a_section_per_stock_and_pipeline_status() -> None:
    tcs = Stock("TCS", "TCS.NS", "TCS", "IT", ("TCS",))
    alerts = pd.DataFrame(
        [
            {
                "alert_date": DAY,
                "symbol": "INFY",
                "severity": "normal",
                "text": "INFY rose 4.0% on 24 Sep.",
            }
        ]
    )
    runs = pd.DataFrame(
        [{"started_at": pd.Timestamp("2026-09-24 10:45", tz=UTC), "exit_code": 0, "failures": ""}]
    )
    text = al.digest(DAY, [INFY, tcs], alerts, runs)
    assert "INFY:\n  - INFY rose 4.0% on 24 Sep." in text
    assert "TCS: quiet day." in text
    assert "Pipeline: run at 16:15 IST completed without failures." in text
    assert not al.FORBIDDEN_RE.search(text)
    empty = al.digest(DAY, [INFY], alerts.iloc[0:0], runs.iloc[0:0])
    assert "INFY: quiet day." in empty and "no run recorded" in empty


def test_run_end_to_end_on_a_database(engine: Engine) -> None:
    """Real readers and tables, synthetic rows: catches column contract mismatches."""
    fetched = pd.Timestamp("2026-09-24 11:00", tz=UTC)
    for symbol, last in (("INFY", 104.0), ("NIFTY50", 100.0)):
        db.upsert_prices(
            bars(last).assign(symbol=symbol, adj_close=lambda d: d["close"], fetched_at=fetched)
        )
    db.insert_filing(
        {
            "id": "f1",
            "exchange": "NSE",
            "exchange_id": "x",
            "symbol": "INFY",
            "filed_at": pd.Timestamp("2026-09-23 18:30", tz=UTC).to_pydatetime(),
            "first_seen_at": fetched.to_pydatetime(),
            "filing_type": "board_meeting",
            "subject": "Board meeting",
        }
    )
    db.record_pipeline_run(
        {
            "started_at": pd.Timestamp("2026-09-24 10:45", tz=UTC).to_pydatetime(),
            "finished_at": fetched.to_pydatetime(),
            "exit_code": 1,
            "failures": "news: sentiment\n",
        }
    )
    assert al.run([INFY], [DAY]) == 2  # the 4% move and the failed run
    assert al.run([INFY], [DAY]) == 0  # re-run: nothing new
    stored = db.read_alerts(DAY, DAY).set_index("alert_type")
    assert "Filing that day: Board meeting." in stored.at["price_move", "text"]
    text = al.digest(DAY, [INFY], db.read_alerts(DAY, DAY), db.read_pipeline_runs())
    assert "had failures: news: sentiment" in text
