"""Social posts -> stock links -> FinBERT sentiment -> social_daily aggregates.

Linking. A post in a stock's dedicated topic (config/social_sources.yaml) links to that
stock with confidence THREAD_CONFIDENCE, without the entity linker. A dedicated topic with
`default_until` (TMPV's Tata Motors thread, 2025-10-14) only does this for posts created
before that IST date; later posts go through the entity linker, whose conditional
"Tata Motors" rule then applies. Posts in general topics (configured without a stock, or
discovered via /latest.json) always go through the entity linker, on the post text only.

Scoring. As for news: only the sentences that mention the stock (at most
`sentiment.max_sentences` from config/news_sources.yaml). A post linked by its thread
that never names the stock ("results were decent") is scored on its first sentences.

Daily aggregates (social_daily). Each post is assigned to the IST trading session it can
first affect (processing/sentiment.py's rules; post time = min(created_at, first_seen_at)).
post_count counts linked posts, author_count distinct author hashes; mean/weighted scores
use the scored posts only. The table is rebuilt from scratch every run.

Run with:  uv run python -m processing.social [--full] [--report]
"""

import argparse
import datetime as dt
import logging
import sys
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config.loader import Stock, load_watchlist
from config.news_sources import NewsConfig, load_news_sources
from config.social_sources import THREAD_CONFIDENCE, ValuePickrConfig, load_social_sources
from processing.entities import (
    LINK_THRESHOLD,
    SENTENCE_BREAK_RE,
    StockMatcher,
    link_article,
)
from processing.sentiment import (
    LABELS,
    FinbertScorer,
    Scorer,
    TradingCalendar,
    mention_sentences,
    news_time,
    session_for,
)
from storage.db import init_db, trading_dates
from storage.social import (
    insert_social_sentiment,
    posts_to_link,
    read_linked_posts,
    read_social_daily,
    replace_social_daily,
    replace_social_mentions,
    social_mentions_to_score,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
VALUEPICKR = "valuepickr"


def post_date(created_at: Any) -> dt.date:
    """IST calendar date of a post (date-dependent rules use exchange dates)."""
    return pd.Timestamp(created_at).tz_convert(IST).date()


# --- linking -----------------------------------------------------------------------


def link_post(
    platform: str,
    topic_id: str,
    text: str | None,
    created: dt.date,
    config: ValuePickrConfig,
    matchers: list[StockMatcher],
) -> list[dict[str, Any]]:
    """Mentions for one post: symbol, method, matched_alias, confidence."""
    topic = config.topic(int(topic_id)) if platform == VALUEPICKR else None
    symbol = topic.default_symbol(created) if topic else None
    if symbol:
        return [{"symbol": symbol, "method": "thread", "matched_alias": None,
                 "confidence": THREAD_CONFIDENCE}]  # fmt: skip
    return [
        {
            "symbol": m["symbol"],
            "method": "linker",
            "matched_alias": m["matched_alias"],
            "confidence": m["confidence"],
        }
        for m in link_article("", None, text, created, matchers)
    ]


def link(stocks: list[Stock], config: ValuePickrConfig, full: bool = False) -> int:
    """Link posts needing it and store their mentions. Returns posts processed."""
    posts = posts_to_link(full=full)
    matchers = [StockMatcher(s) for s in stocks]
    rows = []
    for p in posts.itertuples(index=False):
        date = post_date(p.created_at)
        for mention in link_post(p.platform, p.topic_id, p.text, date, config, matchers):
            rows.append({"post_id": p.id, **mention})
    replace_social_mentions(posts["id"].tolist(), rows)
    linked = sum(1 for r in rows if r["confidence"] >= LINK_THRESHOLD)
    logger.info("Linked %d post(s): %d mention(s), %d at confidence >= %.2f",
                len(posts), len(rows), linked, LINK_THRESHOLD)  # fmt: skip
    return len(posts)


# --- scoring -----------------------------------------------------------------------


def leading_sentences(text: str, limit: int) -> list[str]:
    """The first `limit` sentences (or lines) of `text`."""
    parts = (s.strip() for s in SENTENCE_BREAK_RE.split(text or ""))
    return [s for s in parts if s][:limit]


def build_post_input(
    text: str | None, matcher: StockMatcher, created: dt.date, method: str, max_sentences: int
) -> str:
    """The sentences of a post that mention the stock; for thread-linked posts that
    never name it, the post's first sentences. Empty if there's nothing to score."""
    sentences = mention_sentences(text, matcher, created, max_sentences)
    if not sentences and method == "thread":
        sentences = leading_sentences(text or "", max_sentences)
    return " ".join(sentences)


def score_pending(news_config: NewsConfig, stocks: list[Stock], scorer: Scorer) -> int:
    """Score every linked, not-yet-scored (post, stock) pair. Returns rows written."""
    pending = social_mentions_to_score(scorer.model_name, LINK_THRESHOLD)
    matchers = {s.symbol: StockMatcher(s) for s in stocks}
    pending = pending[pending["symbol"].isin(matchers)]
    inputs = [
        (
            r,
            build_post_input(
                r.text,
                matchers[r.symbol],
                post_date(r.created_at),
                r.method,
                news_config.sentiment_max_sentences,
            ),  # fmt: skip
        )
        for r in pending.itertuples(index=False)
    ]
    inputs = [(r, text) for r, text in inputs if text]
    if not inputs:
        logger.info("No new social mentions to score")
        return 0
    probabilities = scorer.score([text for _, text in inputs])
    now = dt.datetime.now(dt.UTC)
    rows = [
        {
            "post_id": r.post_id,
            "symbol": r.symbol,
            "model_name": scorer.model_name,
            "model_version": scorer.model_version,
            "label": max(LABELS, key=lambda label: p[label]),
            "p_positive": p["positive"],
            "p_negative": p["negative"],
            "p_neutral": p["neutral"],
            "score": p["positive"] - p["negative"],
            "computed_at": now,
        }
        for (r, _), p in zip(inputs, probabilities, strict=True)
    ]
    insert_social_sentiment(rows)
    logger.info("Scored %d social mention(s) with %s", len(rows), scorer.model_name)
    return len(rows)


# --- daily aggregation -------------------------------------------------------------


def aggregate_daily(
    linked: pd.DataFrame,
    calendar: TradingCalendar,
    model_name: str,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """social_daily rows from linked posts (see module docstring)."""
    if linked.empty:
        return []
    df = linked.copy()
    df["session_date"] = [
        session_for(news_time(c, f), calendar)
        for c, f in zip(df["created_at"], df["first_seen_at"], strict=True)
    ]
    df["first_seen_at"] = pd.to_datetime(df["first_seen_at"], utc=True)
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df["weighted"] = df["score"] * df["confidence"]
    df["scored_confidence"] = df["confidence"].where(df["score"].notna())
    daily = df.groupby(["platform", "symbol", "session_date"]).agg(
        post_count=("post_id", "nunique"),
        author_count=("author_hmac", "nunique"),
        scored_count=("score", "count"),
        mean_score=("score", "mean"),
        weighted_sum=("weighted", "sum"),
        confidence_sum=("scored_confidence", "sum"),
        latest_first_seen_at=("first_seen_at", "max"),
    )
    computed_at = now or dt.datetime.now(dt.UTC)
    rows = []
    for (platform, symbol, session_date), r in daily.iterrows():
        scored = r.scored_count > 0
        rows.append(
            {
                "platform": platform,
                "symbol": symbol,
                "session_date": session_date,
                "model_name": model_name,
                "post_count": int(r.post_count),
                "author_count": int(r.author_count),
                "scored_count": int(r.scored_count),
                "mean_score": float(r.mean_score) if scored else None,
                "weighted_score": float(r.weighted_sum / r.confidence_sum) if scored else None,
                "latest_first_seen_at": r.latest_first_seen_at.to_pydatetime(),
                "computed_at": computed_at,
            }
        )
    return rows


def rebuild_daily(news_config: NewsConfig) -> int:
    """Recompute social_daily for the configured model from all linked posts."""
    model = news_config.sentiment_model
    rows = aggregate_daily(
        read_linked_posts(model, LINK_THRESHOLD), TradingCalendar(trading_dates()), model
    )
    replace_social_daily(model, rows)
    logger.info("Rebuilt social_daily: %d stock-session row(s)", len(rows))
    return len(rows)


def log_report(news_config: NewsConfig, days: int = 7) -> None:
    """Log posts (authors) and weighted score for the last `days` sessions per stock."""
    daily = read_social_daily(news_config.sentiment_model)
    if daily.empty:
        logger.info("No social_daily rows yet")
        return
    recent = sorted(daily["session_date"].unique())[-days:]
    view = daily[daily["session_date"].isin(recent)].copy()
    view["cell"] = [
        f"{r.post_count} ({r.author_count})"
        + (f" {r.weighted_score:+.2f}" if pd.notna(r.weighted_score) else "")
        for r in view.itertuples()
    ]
    table = view.pivot_table(index="session_date", columns="symbol", values="cell",
                             aggfunc="first").fillna("-")  # fmt: skip
    logger.info("Posts (authors) weighted score, by session:\n%s", table.to_string())


def main(argv: list[str] | None = None) -> int:
    """Entry point: link new posts, score them, rebuild social_daily."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="re-link all posts")
    parser.add_argument("--report", action="store_true", help="print recent sessions")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    news_config = load_news_sources()
    stocks = load_watchlist()
    link(stocks, load_social_sources(), full=args.full)
    scorer = FinbertScorer(news_config.sentiment_model, news_config.sentiment_revision,
                           news_config.sentiment_batch_size)  # fmt: skip
    score_pending(news_config, stocks, scorer)
    rebuild_daily(news_config)
    if args.report:
        log_report(news_config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
