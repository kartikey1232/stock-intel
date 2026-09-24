"""Daily alerts and digest (Phase 6): attention flags built from the day's stored data.

Alert types and thresholds are in config/alerts.yaml. For a session date d:

- results: a results filing first seen on d, for the stock's latest quarter, whose board
  date is at most `max_age_days` old (so backfilled history never alerts), with YoY/QoQ
  headline figures.
- price_move: |close-to-close move| above `threshold_pct`, or a gap signal, with that
  session's news count and sentiment and any filing dated d.
- price_signal: the configured signals on d. SMA crosses respect min_gap_pct (shown on
  their confirmation day, as on the dashboard).
- news_shift: the 7-day story-weighted sentiment moves at least `threshold` away from the
  30-day average (only on the first day it does), with the top stories in that direction.
- pending_action: corporate actions detected in filings with a configured status (only
  for the latest date; there's no history of past statuses).
- pipeline: failed steps of run_update.py runs started on d.

Every price-signal alert carries a one-line note of that signal's event-study result
(processing/backtest.py, in sample, plus any out-of-sample status from
config/alerts.yaml), so a signal reads as an attention flag. No alert text may recommend
buying or selling: FORBIDDEN_RE is enforced by the tests, and headlines that read like
recommendations are never quoted.

Alerts are stored in `alerts`, keyed on (symbol, alert_type, subject), so re-running a
day never stores an alert twice. When days are rebuilt afterwards, the backtest note
uses today's full event study (a note on a past day can include later events).

Run with:  uv run python -m processing.alerts [--date YYYY-MM-DD] [--days N] [--digest]
"""

import argparse
import datetime as dt
import logging
import re
import sys
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

from config.alerts import AlertsConfig, load_alerts_config
from config.loader import Stock, load_watchlist
from config.market_calendar import latest_completed_session, load_holidays
from config.news_sources import load_news_sources
from config.signals import SignalRule, load_signals
from processing.adjustments import adjust_prices
from processing.backtest import TOO_FEW, run_event_study
from processing.entities import LINK_THRESHOLD
from processing.results import changes
from processing.sentiment import TradingCalendar, news_time, session_for, story_weighted_average
from processing.signals import describe_signal_value, display_signals
from storage.db import (
    init_db,
    insert_new_alerts,
    read_alerts,
    read_corporate_actions,
    read_filings_for_alerts,
    read_news_daily,
    read_pending_actions,
    read_pipeline_runs,
    read_prices,
    read_results,
    read_signals,
    read_stock_news,
    trading_dates,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
PIPELINE_SYMBOL = "*"
GAP_SIGNALS = ("gap_up", "gap_down")
# Words that would make an alert read as advice. Alert text must never match.
FORBIDDEN_RE = re.compile(
    r"\b(?:buy|buying|sell|selling|sold|accumulate|go long|go short|shorting|target price|"
    r"recommend\w*|stop[- ]loss|take profit)\b",
    re.IGNORECASE,
)
# Third-party headlines that read like tips are never quoted in alerts.
TIP_HEADLINE_RE = re.compile(
    rf"{FORBIDDEN_RE.pattern}|\b(?:stocks? to|top picks?|picks?\b|multibagger|upside of)",
    re.IGNORECASE,
)


@dataclass
class Context:
    """Everything the builders need, loaded once per run."""

    stocks: list[Stock]
    config: AlertsConfig
    signal_rules: list[SignalRule]
    model_name: str
    prices: dict[str, pd.DataFrame] = field(default_factory=dict)
    signals: dict[str, pd.DataFrame] = field(default_factory=dict)
    news_daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    news: dict[str, pd.DataFrame] = field(default_factory=dict)
    filings: pd.DataFrame = field(default_factory=pd.DataFrame)
    results: pd.DataFrame = field(default_factory=pd.DataFrame)
    pending: pd.DataFrame = field(default_factory=pd.DataFrame)
    runs: pd.DataFrame = field(default_factory=pd.DataFrame)
    backtest: pd.DataFrame = field(default_factory=pd.DataFrame)
    calendar: TradingCalendar = field(default_factory=lambda: TradingCalendar([]))


def load_context(stocks: list[Stock]) -> Context:
    """Read every input table once."""
    ctx = Context(stocks, load_alerts_config(), load_signals(), load_news_sources().sentiment_model)
    stored_signals = read_signals()
    for stock in stocks:
        bars = adjust_prices(read_prices(stock.symbol), read_corporate_actions(stock.symbol))
        ctx.prices[stock.symbol] = bars
        own = stored_signals[stored_signals["symbol"] == stock.symbol]
        ctx.signals[stock.symbol] = display_signals(own, bars, ctx.signal_rules)
        ctx.news[stock.symbol] = read_stock_news(stock.symbol, ctx.model_name, LINK_THRESHOLD)
    ctx.news_daily = read_news_daily(ctx.model_name)
    ctx.filings, ctx.results = read_filings_for_alerts(), read_results()
    ctx.pending, ctx.runs = read_pending_actions(), read_pipeline_runs()
    ctx.calendar = TradingCalendar(trading_dates())
    ctx.backtest, _ = run_event_study(stocks)
    return ctx


# --- text helpers ------------------------------------------------------------------


def ist_date(moment: object) -> dt.date:
    """The IST calendar date of a stored UTC timestamp."""
    stamp = pd.Timestamp(moment)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
    return stamp.tz_convert(IST).date()


def ist_dates(moments: pd.Series) -> pd.Series:
    """IST calendar dates of stored UTC timestamps (object dtype, even when empty)."""
    stamps = pd.to_datetime(moments, utc=True)
    return pd.Series([s.tz_convert(IST).date() for s in stamps], index=moments.index, dtype=object)


def backtest_note(ctx: Context, signal: str) -> str:
    """One line on how this signal did historically, as an attention flag."""
    h = ctx.config.note_horizon
    label = next((r.label for r in ctx.signal_rules if r.name == signal), signal)
    rows = ctx.backtest
    row = rows[(rows["signal"] == signal) & (rows["horizon"] == h)] if len(rows) else rows
    if row.empty:
        text = "no backtest result yet"
    else:
        r = row.iloc[0]
        n = int(r["n"])
        text = {
            TOO_FEW: f"too few past events to judge (n={n})",
            "no clear difference": f"historically no edge vs doing nothing at {h} trading days "
            f"(n={n})",
            "beats baseline (CI > 0)": f"historically moved further in the signal's direction "
            f"than an average day at {h} trading days (n={n}), possibly by chance",
            "worse than baseline (CI < 0)": f"historically moved less in the signal's direction "
            f"than an average day at {h} trading days (n={n}), possibly by chance",
        }.get(r["note"], f"n={n}")
    oos = ctx.config.out_of_sample.get(signal)
    return f"Backtest ({label}): {text}{'; ' + oos if oos else ''}."


def pct(value: float | None) -> str:
    """A fraction as a signed percentage, or n/a."""
    return "n/a" if value is None or pd.isna(value) else f"{value * 100:+.1f}%"


def alert(
    ctx: Context, symbol: str, alert_type: str, subject: str, day: dt.date, text: str
) -> dict:
    """An alerts row."""
    return {
        "symbol": symbol,
        "alert_type": alert_type,
        "subject": subject,
        "alert_date": day,
        "severity": ctx.config.rules[alert_type].severity,
        "text": text,
        "created_at": dt.datetime.now(dt.UTC),
        "sent_at": None,
    }


# --- builders ----------------------------------------------------------------------


def session_news(ctx: Context, symbol: str, day: dt.date) -> str:
    """That session's news count and sentiment, as a sentence."""
    daily = ctx.news_daily
    if daily.empty:
        return "No linked news that session."
    row = daily[
        (daily["symbol"] == symbol) & (pd.to_datetime(daily["session_date"]).dt.date == day)
    ]
    if row.empty:
        return "No linked news that session."
    r = row.iloc[0]
    plural = "story" if r["story_count"] == 1 else "stories"
    return f"News that session: {r['story_count']} {plural}, sentiment {r['weighted_score']:+.2f}."


def day_filings(ctx: Context, symbol: str, day: dt.date) -> str:
    """Filings dated `day`, as a sentence (empty if none)."""
    f = ctx.filings
    if f.empty:
        return ""
    own = f[(f["symbol"] == symbol) & (f["filed_at"].pipe(ist_dates) == day)]
    if own.empty:
        return ""
    return "Filing that day: " + "; ".join(str(s) for s in own["subject"]) + "."


def price_move_alerts(ctx: Context, stock: Stock, day: dt.date) -> list[dict]:
    """A big close-to-close move or a gap signal on `day`."""
    rule = ctx.config.rule("price_move")
    bars = ctx.prices.get(stock.symbol, pd.DataFrame())
    if rule is None or bars.empty:
        return []
    bars = bars.sort_values("date").reset_index(drop=True)
    at = bars.index[bars["date"].dt.date == day]
    if len(at) == 0 or at[0] == 0:
        return []
    i = at[0]
    close, prev = bars.at[i, "close"], bars.at[i - 1, "close"]
    move = close / prev - 1
    sig = ctx.signals.get(stock.symbol, pd.DataFrame())
    gaps = sig[(pd.to_datetime(sig["date"]).dt.date == day) & sig["signal"].isin(GAP_SIGNALS)]
    if abs(move) * 100 <= rule.params["threshold_pct"] and gaps.empty:
        return []
    parts = [
        f"{stock.symbol} {'rose' if move > 0 else 'fell'} {abs(move) * 100:.1f}% on "
        f"{day:%d %b} (close ₹{close:,.2f})."
    ]
    for g in gaps.itertuples():
        side = "above" if g.signal == "gap_up" else "below"
        parts.append(f"It opened {abs(g.value):.1f}% {side} the previous close.")
    parts.append(session_news(ctx, stock.symbol, day))
    parts.append(day_filings(ctx, stock.symbol, day))
    if gaps.empty:
        parts.append("A price move on its own isn't a tested signal.")
    for g in gaps.itertuples():
        parts.append(backtest_note(ctx, g.signal))
    text = " ".join(p for p in parts if p)
    return [alert(ctx, stock.symbol, "price_move", f"{day}:price_move", day, text)]


def price_signal_alerts(ctx: Context, stock: Stock, day: dt.date) -> list[dict]:
    """Configured signals (breakouts, SMA crosses) on `day`."""
    rule = ctx.config.rule("price_signal")
    sig = ctx.signals.get(stock.symbol, pd.DataFrame())
    if rule is None or sig.empty:
        return []
    today = sig[
        (pd.to_datetime(sig["date"]).dt.date == day) & sig["signal"].isin(rule.params["signals"])
    ]
    by_name = {r.name: r for r in ctx.signal_rules}
    rows = []
    for s in today.itertuples():
        r = by_name[s.signal]
        confirmed = " (confirmed)" if r.params.get("min_gap_pct", 0) > 0 else ""
        text = (
            f"{stock.symbol}: {r.label}{confirmed} on {day:%d %b}, "
            f"{describe_signal_value(r.type, s.value)}. {backtest_note(ctx, s.signal)}"
        )
        rows.append(alert(ctx, stock.symbol, "price_signal", f"{day}:{s.signal}", day, text))
    return rows


def story_rows(ctx: Context, symbol: str) -> pd.DataFrame:
    """One row per linked news story (earliest copy), with its session and mean score."""
    articles = ctx.news.get(symbol, pd.DataFrame())
    if articles.empty:
        return articles
    df = articles.copy()
    df["story"] = df["story_id"].fillna(df["article_id"])
    df["news_time"] = [
        news_time(p, f) for p, f in zip(df["published_at"], df["first_seen_at"], strict=True)
    ]
    df = df.sort_values("news_time")
    stories = df.groupby("story", sort=False).first()
    stories["score"] = df.groupby("story")["score"].mean()
    stories["session_date"] = [session_for(t, ctx.calendar) for t in stories["news_time"]]
    return stories.reset_index()


def shift_at(ctx: Context, symbol: str, day: dt.date) -> tuple[float | None, float | None, int]:
    """(7-day average, 30-day average, stories in 7 days) up to `day`."""
    daily = (
        ctx.news_daily[ctx.news_daily["symbol"] == symbol]
        if len(ctx.news_daily)
        else ctx.news_daily
    )
    if daily.empty:
        return None, None, 0
    dates = pd.to_datetime(daily["session_date"]).dt.date
    week = daily[(dates > day - dt.timedelta(days=7)) & (dates <= day)]
    return (
        story_weighted_average(daily, day, 7),
        story_weighted_average(daily, day, 30),
        int(week["story_count"].sum()),
    )


def is_shift(avg7: float | None, avg30: float | None, stories: int, params: dict) -> bool:
    """True if the 7-day average is far enough from the 30-day one, on enough stories."""
    return (
        avg7 is not None
        and avg30 is not None
        and stories >= params["min_stories_7d"]
        and abs(avg7 - avg30) >= params["threshold"]
    )


def news_shift_alerts(ctx: Context, stock: Stock, day: dt.date) -> list[dict]:
    """First day the 7-day news sentiment moves away from the 30-day average."""
    rule = ctx.config.rule("news_shift")
    if rule is None:
        return []
    avg7, avg30, stories = shift_at(ctx, stock.symbol, day)
    if not is_shift(avg7, avg30, stories, rule.params):
        return []
    previous = ctx.calendar.previous_trading_day(day)
    if is_shift(*shift_at(ctx, stock.symbol, previous), rule.params):
        return []  # already shifted yesterday: alerted then
    direction = 1 if avg7 > avg30 else -1
    rows = story_rows(ctx, stock.symbol)
    if not rows.empty:
        sessions = pd.Series(rows["session_date"])
        rows = rows[(sessions > day - dt.timedelta(days=7)) & (sessions <= day)]
        rows = rows[rows["score"].notna() & ~rows["title"].str.contains(TIP_HEADLINE_RE)]
        rows = rows.assign(rank=rows["score"] * direction).sort_values("rank", ascending=False)
    top = rows.head(rule.params["top_headlines"]) if not rows.empty else rows
    heads = "; ".join(f'"{r.title}" ({r.source or "unknown"})' for r in top.itertuples())
    tone = "more positive" if direction > 0 else "more negative"
    text = (
        f"{stock.symbol}: news sentiment over the last 7 days is {avg7:+.2f}, vs {avg30:+.2f} "
        f"over 30 days ({stories} stories, {tone})." + (f" Top stories: {heads}." if heads else "")
    )
    return [alert(ctx, stock.symbol, "news_shift", f"{day}:news_shift", day, text)]


def headline_figure(name: str, row: pd.Series) -> str:
    """'revenue ₹48,211 cr (YoY +14.0%, QoQ +3.9%)'."""
    return f"{name} ₹{row['value']:,.0f} cr (YoY {pct(row['yoy'])}, QoQ {pct(row['qoq'])})"


def results_alerts(ctx: Context, stock: Stock, day: dt.date) -> list[dict]:
    """Newly imported results for the latest quarter, with headline YoY/QoQ figures."""
    rule = ctx.config.rule("results")
    f, res = ctx.filings, ctx.results
    if rule is None or f.empty or res.empty:
        return []
    own = f[(f["symbol"] == stock.symbol) & (f["filing_type"] == "results")]
    own = own[own["first_seen_at"].pipe(ist_dates) == day]
    own = own[
        own["filed_at"].pipe(ist_dates) >= day - dt.timedelta(days=rule.params["max_age_days"])
    ]
    stock_results = res[res["symbol"] == stock.symbol]
    if own.empty or stock_results.empty:
        return []
    latest = pd.to_datetime(stock_results["period_end"]).max().date()
    diff = changes(stock_results)
    rows = []
    for filing in own.itertuples():
        mine = stock_results[stock_results["filing_id"] == filing.id]
        if mine.empty or pd.to_datetime(mine["period_end"]).max().date() != latest:
            continue
        basis, quarter = mine["basis"].iloc[0], mine["fiscal_quarter"].iloc[0]
        d = diff[(diff["basis"] == basis) & (diff["period_end"] == latest)].set_index("metric")
        top = "revenue" if "revenue" in d.index else "total_income"
        figures = [
            headline_figure(name, d.loc[m])
            for m, name in ((top, top.replace("_", " ")), ("net_profit", "net profit"))
            if m in d.index
        ]
        text = (
            f"{stock.symbol}: {quarter} {basis} results imported (board date "
            f"{ist_date(filing.filed_at):%d %b %Y}). " + "; ".join(figures) + "."
        )
        rows.append(alert(ctx, stock.symbol, "results", str(filing.id), day, text))
    return rows


def pending_alerts(ctx: Context, stock: Stock, day: dt.date) -> list[dict]:
    """Corporate actions detected in filings that may need config/corporate_actions.yaml."""
    rule = ctx.config.rule("pending_action")
    p = ctx.pending
    if rule is None or p.empty:
        return []
    own = p[(p["symbol"] == stock.symbol) & p["status"].isin(rule.params["statuses"])]
    return [
        alert(
            ctx,
            stock.symbol,
            "pending_action",
            str(a.id),
            day,
            f"{stock.symbol}: {a.action_type}{' ' + a.ratio if a.ratio else ''} detected in a "
            f"filing (ex-date {a.ex_date or 'not stated'}), status {a.status}. Check whether "
            "config/corporate_actions.yaml needs it.",
        )
        for a in own.itertuples()
    ]


def pipeline_alerts(ctx: Context, day: dt.date) -> list[dict]:
    """Failed run_update.py runs started on `day`."""
    rule = ctx.config.rule("pipeline")
    runs = ctx.runs
    if rule is None or runs.empty:
        return []
    failed = runs[(runs["started_at"].pipe(ist_dates) == day) & (runs["exit_code"] != 0)]
    return [
        alert(
            ctx,
            PIPELINE_SYMBOL,
            "pipeline",
            f"run:{pd.Timestamp(r.started_at).isoformat()}",
            day,
            f"run_update at {pd.Timestamp(r.started_at).tz_convert(IST):%H:%M} IST failed: "
            + ", ".join(line for line in r.failures.splitlines() if line)
            + ".",
        )
        for r in failed.itertuples()
    ]


def build_alerts(ctx: Context, day: dt.date, latest: bool) -> list[dict]:
    """Every alert for session `day`. Pending actions only when `latest` (no history)."""
    rows = []
    for stock in ctx.stocks:
        rows += results_alerts(ctx, stock, day)
        rows += price_move_alerts(ctx, stock, day)
        rows += price_signal_alerts(ctx, stock, day)
        rows += news_shift_alerts(ctx, stock, day)
        if latest:
            rows += pending_alerts(ctx, stock, day)
    rows += pipeline_alerts(ctx, day)
    bad = [r for r in rows if FORBIDDEN_RE.search(r["text"])]
    if bad:  # a template or headline slipped through: never store advice-like text
        raise ValueError(f"alert text reads like advice: {bad[0]['text']!r}")
    return rows


# --- digest ------------------------------------------------------------------------


def pipeline_status(runs: pd.DataFrame, day: dt.date) -> str:
    """The last run_update.py run started on `day`, as a line."""
    today = runs[runs["started_at"].pipe(ist_dates) == day] if len(runs) else runs
    if today.empty:
        return "Pipeline: no run recorded for this day."
    r = today.iloc[-1]
    at = pd.Timestamp(r["started_at"]).tz_convert(IST)
    if r["exit_code"] == 0:
        return f"Pipeline: run at {at:%H:%M} IST completed without failures."
    failures = ", ".join(line for line in r["failures"].splitlines() if line)
    return f"Pipeline: run at {at:%H:%M} IST had failures: {failures}."


def digest(day: dt.date, stocks: list[Stock], alerts: pd.DataFrame, runs: pd.DataFrame) -> str:
    """Template digest for `day`: a section per stock, then pipeline status."""
    lines = [
        f"stock-intel digest for {day:%a %d %b %Y}",
        "Attention flags from stored data, not advice.",
        "",
    ]
    today = alerts[alerts["alert_date"] == day] if len(alerts) else alerts
    for stock in stocks:
        own = today[today["symbol"] == stock.symbol] if len(today) else today
        if own.empty:
            lines.append(f"{stock.symbol}: quiet day.")
            continue
        lines.append(f"{stock.symbol}:")
        for a in own.sort_values("severity").itertuples():
            lines.append(f"  {'[!] ' if a.severity == 'high' else '- '}{a.text}")
    lines += ["", pipeline_status(runs, day)]
    return "\n".join(lines)


# --- orchestration -----------------------------------------------------------------


def recent_sessions(n: int, until: dt.date) -> list[dt.date]:
    """The last `n` stored trading dates up to `until`."""
    return [d for d in trading_dates() if d <= until][-n:]


def run(stocks: list[Stock], days: list[dt.date]) -> int:
    """Build and store alerts for each day (latest day also gets pending actions).
    Returns alerts newly stored."""
    ctx = load_context(stocks)
    stored = 0
    for day in days:
        rows = build_alerts(ctx, day, latest=day == max(days))
        new = insert_new_alerts(rows)
        stored += new
        logger.info("Alerts for %s: %d built, %d new", day, len(rows), new)
    return stored


def main(argv: list[str] | None = None) -> int:
    """Entry point: build alerts for a day (or the last N days) and optionally print the
    digest for the last of them."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=dt.date.fromisoformat, help="session date (default: latest)")
    parser.add_argument("--days", type=int, default=1, help="build the last N trading days")
    parser.add_argument("--digest", action="store_true", help="print the digest")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    until = args.date or latest_completed_session(dt.datetime.now(dt.UTC), load_holidays())
    days = recent_sessions(args.days, until)
    stocks = load_watchlist()
    run(stocks, days)
    if args.digest:
        print(digest(days[-1], stocks, read_alerts(days[-1], days[-1]), read_pipeline_runs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
