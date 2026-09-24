"""News sentiment per (article, stock) with FinBERT, and daily per-stock aggregates.

Scoring. For each linked mention (confidence >= entities.LINK_THRESHOLD) not yet scored
by the configured model, the input is the title plus the sentences in the summary and
body that mention the stock (at most `max_sentences`), not the whole article, which may
be about something else. The model is loaded once and run in CPU batches; its exact
revision is stored as model_version. score = p_positive - p_negative, in [-1, 1].

Daily aggregates (news_daily). Each article is assigned to the IST trading session it
can first affect: anything after 15:30 IST, or on a weekend or exchange holiday, moves to
the next trading day. Trading days are the dates we hold price bars for; dates after the
last stored bar are assumed to be weekdays-only until prices arrive, and aggregates are
rebuilt every run, so holiday assignments self-correct. Copies of one story count once:
a story takes the session of its earliest copy and the mean score of its copies.

Run with:  uv run python -m processing.sentiment [--report]
"""

import argparse
import bisect
import datetime as dt
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from config.loader import Stock, load_watchlist
from config.news_sources import NewsConfig, load_news_sources
from processing.entities import LINK_THRESHOLD, StockMatcher, event_date_of, sentence_at
from storage.db import (
    init_db,
    insert_sentiment,
    mentions_to_score,
    read_news_daily,
    read_scored_mentions,
    replace_news_daily,
    trading_dates,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = dt.time(15, 30)
LABELS = ("positive", "negative", "neutral")


class Scorer(Protocol):
    """Anything that turns texts into label probabilities."""

    model_name: str
    model_version: str

    def score(self, texts: Sequence[str]) -> list[dict[str, float]]:
        """Return {'positive': p, 'negative': p, 'neutral': p} per text."""
        ...


class FinbertScorer:
    """FinBERT on CPU, loaded once. Torch and transformers are imported lazily."""

    def __init__(self, model_name: str, revision: str | None, batch_size: int) -> None:
        # FinBERT ships only pytorch_model.bin, so on every load transformers would start
        # a background thread asking the Hub to convert it to safetensors, even when the
        # model is cached. Opt out: loads stay offline and nothing logs after exit.
        os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")
        import torch

        self._torch = torch
        self.model_name = model_name
        self.batch_size = batch_size
        try:  # use the local cache without touching the network when we can
            self.tokenizer, self.model = self._load(revision, local_files_only=True)
        except OSError:
            logger.info("Downloading %s@%s", model_name, revision or "latest")
            self.tokenizer, self.model = self._load(revision, local_files_only=False)
        self.model_version = getattr(self.model.config, "_commit_hash", None) or revision or ""
        self.id2label = {i: label.lower() for i, label in self.model.config.id2label.items()}
        if set(self.id2label.values()) != set(LABELS):
            raise ValueError(f"{model_name} labels {self.id2label} are not {LABELS}")

    def _load(self, revision: str | None, local_files_only: bool) -> tuple[Any, Any]:
        """Load tokenizer and model for self.model_name at `revision`."""
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        kwargs = {"revision": revision, "local_files_only": local_files_only}
        tokenizer = AutoTokenizer.from_pretrained(self.model_name, **kwargs)
        model = AutoModelForSequenceClassification.from_pretrained(self.model_name, **kwargs)
        return tokenizer, model.eval()

    def score(self, texts: Sequence[str]) -> list[dict[str, float]]:
        """Label probabilities for each text, in batches."""
        results: list[dict[str, float]] = []
        with self._torch.no_grad():
            for i in range(0, len(texts), self.batch_size):
                batch = self.tokenizer(
                    list(texts[i : i + self.batch_size]),
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                )
                probs = self._torch.softmax(self.model(**batch).logits, dim=-1).tolist()
                results += [{self.id2label[j]: p for j, p in enumerate(row)} for row in probs]
        return results


# --- scoring -----------------------------------------------------------------------


def mention_sentences(
    text: str | None, matcher: StockMatcher, event_date: dt.date, limit: int
) -> list[str]:
    """Distinct sentences of `text` in which `matcher`'s stock is validly mentioned."""
    sentences: list[str] = []
    for _, pos, _end in matcher.matches(text or "", event_date):
        sentence = sentence_at(text, pos).strip()
        if sentence and sentence not in sentences:
            sentences.append(sentence)
        if len(sentences) >= limit:
            break
    return sentences


def build_input(
    title: str,
    summary: str | None,
    body: str | None,
    matcher: StockMatcher,
    event_date: dt.date,
    max_sentences: int,
) -> str:
    """Title plus the summary/body sentences that mention the stock."""
    parts = [title.strip()]
    for text in (summary, body):
        remaining = max_sentences - (len(parts) - 1)
        if remaining <= 0:
            break
        for sentence in mention_sentences(text, matcher, event_date, remaining):
            if sentence not in parts:
                parts.append(sentence)
    return " ".join(parts)


def score_pending(config: NewsConfig, stocks: list[Stock], scorer: Scorer) -> int:
    """Score every linked, not-yet-scored mention. Returns rows written."""
    pending = mentions_to_score(scorer.model_name, LINK_THRESHOLD)
    if pending.empty:
        logger.info("No new mentions to score")
        return 0
    pending = pending.astype(object).where(pending.notna(), None)
    matchers = {s.symbol: StockMatcher(s) for s in stocks}
    known = pending[pending["symbol"].isin(matchers)]
    if len(known) < len(pending):
        logger.warning("Skipping %d mention(s) of stocks no longer in the watchlist",
                       len(pending) - len(known))  # fmt: skip

    texts = [
        build_input(
            r.title,
            r.summary,
            r.text,
            matchers[r.symbol],
            event_date_of(r.published_at, r.first_seen_at),
            config.sentiment_max_sentences,
        )
        for r in known.itertuples(index=False)
    ]
    probabilities = scorer.score(texts)
    now = dt.datetime.now(dt.UTC)
    rows = [
        {
            "article_id": r.article_id,
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
        for r, p in zip(known.itertuples(index=False), probabilities, strict=True)
    ]
    insert_sentiment(rows)
    logger.info("Scored %d mention(s) with %s@%s", len(rows), scorer.model_name,
                scorer.model_version[:8])  # fmt: skip
    return len(rows)


# --- trading sessions --------------------------------------------------------------


class TradingCalendar:
    """NSE trading days: stored price dates, then weekdays after the last stored bar."""

    def __init__(self, known_days: Sequence[dt.date]) -> None:
        self._known = sorted(set(known_days))
        self._known_set = set(self._known)

    def is_trading_day(self, day: dt.date) -> bool:
        """True if `day` is (or, beyond known data, is assumed to be) a trading day."""
        if self._known and self._known[0] <= day <= self._known[-1]:
            return day in self._known_set
        return day.weekday() < 5

    def previous_trading_day(self, day: dt.date) -> dt.date:
        """The last trading day strictly before `day`."""
        i = bisect.bisect_left(self._known, day)
        if self._known and day <= self._known[-1] and i > 0:
            return self._known[i - 1]
        day -= dt.timedelta(days=1)
        while not self.is_trading_day(day):
            day -= dt.timedelta(days=1)
        return day

    def next_trading_day(self, day: dt.date) -> dt.date:
        """The first trading day strictly after `day`."""
        i = bisect.bisect_right(self._known, day)
        if i < len(self._known):
            return self._known[i]
        day += dt.timedelta(days=1)
        while not self.is_trading_day(day):
            day += dt.timedelta(days=1)
        return day


def session_for(moment: dt.datetime, calendar: TradingCalendar) -> dt.date:
    """IST trading session a piece of news first affects (after 15:30 -> next session)."""
    local = moment.astimezone(IST)
    day = local.date()
    if local.time() > MARKET_CLOSE or not calendar.is_trading_day(day):
        return calendar.next_trading_day(day)
    return day


def news_time(published_at: Any, first_seen_at: Any) -> dt.datetime:
    """When the news became public: published_at, unless missing or later than we saw it."""
    seen = pd.Timestamp(first_seen_at).to_pydatetime()
    if published_at is None or pd.isna(published_at):
        return seen
    published = pd.Timestamp(published_at).to_pydatetime()
    return min(published, seen)


# --- daily aggregation -------------------------------------------------------------


def aggregate_daily(
    scored: pd.DataFrame,
    calendar: TradingCalendar,
    strong_negative: float,
    model_name: str,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> list[dict[str, Any]]:
    """news_daily rows from scored mentions (see module docstring for the rules)."""
    if scored.empty:
        return []
    df = scored.copy()
    df["session_date"] = [
        session_for(news_time(p, s), calendar)
        for p, s in zip(df["published_at"], df["first_seen_at"], strict=True)
    ]
    df["story"] = df["story_id"].fillna(df["article_id"])
    df["first_seen_at"] = pd.to_datetime(df["first_seen_at"], utc=True)

    stories = df.groupby(["symbol", "story"]).agg(
        session_date=("session_date", "min"),
        score=("score", "mean"),
        confidence=("confidence", "max"),
        articles=("article_id", "nunique"),
        latest_first_seen_at=("first_seen_at", "max"),
    )
    stories["weighted"] = stories["score"] * stories["confidence"]
    stories["strong_negative"] = stories["score"] <= strong_negative

    daily = stories.groupby(["symbol", "session_date"]).agg(
        story_count=("score", "size"),
        article_count=("articles", "sum"),
        mean_score=("score", "mean"),
        weighted_sum=("weighted", "sum"),
        confidence_sum=("confidence", "sum"),
        strong_negative_stories=("strong_negative", "sum"),
        latest_first_seen_at=("latest_first_seen_at", "max"),
    )
    daily["weighted_score"] = daily["weighted_sum"] / daily["confidence_sum"]
    computed_at = now()
    return [
        {
            "symbol": symbol,
            "session_date": session_date,
            "model_name": model_name,
            "story_count": int(r.story_count),
            "article_count": int(r.article_count),
            "mean_score": float(r.mean_score),
            "weighted_score": float(r.weighted_score),
            "strong_negative_stories": int(r.strong_negative_stories),
            "latest_first_seen_at": r.latest_first_seen_at.to_pydatetime(),
            "computed_at": computed_at,
        }
        for (symbol, session_date), r in daily.iterrows()
    ]


def rebuild_daily(config: NewsConfig) -> int:
    """Recompute news_daily for the configured model from all scored mentions."""
    rows = aggregate_daily(
        read_scored_mentions(config.sentiment_model),
        TradingCalendar(trading_dates()),
        config.sentiment_strong_negative,
        config.sentiment_model,
    )
    replace_news_daily(config.sentiment_model, rows)
    logger.info("Rebuilt news_daily: %d stock-session row(s)", len(rows))
    return len(rows)


def story_weighted_average(daily: pd.DataFrame, end: dt.date, days: int) -> float | None:
    """Story-weighted mean of news_daily weighted scores over sessions in (end - days, end];
    None if there were no stories."""
    if daily.empty:
        return None
    dates = pd.to_datetime(daily["session_date"]).dt.date
    rows = daily[(dates > end - dt.timedelta(days=days)) & (dates <= end)]
    if rows.empty or rows["story_count"].sum() == 0:
        return None
    return float((rows["weighted_score"] * rows["story_count"]).sum() / rows["story_count"].sum())


# --- report ------------------------------------------------------------------------


def log_report(config: NewsConfig, days: int = 7) -> None:
    """Log the last `days` sessions per stock and the most positive/negative headlines."""
    daily = read_news_daily(config.sentiment_model)
    if daily.empty:
        logger.info("No news_daily rows yet")
        return
    recent = sorted(daily["session_date"].unique())[-days:]
    view = daily[daily["session_date"].isin(recent)]
    table = view.pivot_table(
        index="session_date", columns="symbol", values="weighted_score", aggfunc="first"
    ).round(2)
    counts = view.pivot_table(
        index="session_date", columns="symbol", values="story_count", aggfunc="first"
    )
    combined = table.astype(str) + " (" + counts.fillna(0).astype(int).astype(str) + ")"
    combined = combined.where(table.notna(), "-")
    logger.info("Weighted score (stories) by session:\n%s", combined.to_string())

    scored = read_scored_mentions(config.sentiment_model)
    unique = scored.sort_values("score").drop_duplicates(["title", "symbol"])
    for name, rows in (("most negative", unique.head(5)), ("most positive", unique.tail(5)[::-1])):
        logger.info("5 %s:", name)
        for r in rows.itertuples():
            logger.info("  %+.2f %-9s %s (%s)", r.score, r.symbol, r.title[:100], r.source)


def main(argv: list[str] | None = None) -> int:
    """Entry point: score new mentions, rebuild daily aggregates, optionally report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="print recent scores")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    config = load_news_sources()
    scorer = FinbertScorer(
        config.sentiment_model, config.sentiment_revision, config.sentiment_batch_size
    )
    score_pending(config, load_watchlist(), scorer)
    rebuild_daily(config)
    if args.report:
        log_report(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
