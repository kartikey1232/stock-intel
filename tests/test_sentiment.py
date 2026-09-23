import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.entities as entities
import processing.sentiment as sentiment
from config.loader import Stock
from config.news_sources import NewsConfig
from storage import db

IST = sentiment.IST
INFY = Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys",))
TCS = Stock("TCS", "TCS.NS", "Tata Consultancy Services Ltd", "IT", ("TCS",))
CONFIG = NewsConfig("ua", 5, {}, "https://g", {}, "7d", sentiment_model="fake/model")


class FakeScorer:
    """Keyword-based stand-in for FinBERT that records what it was asked to score."""

    model_name = "fake/model"
    model_version = "abc123"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def score(self, texts: Sequence[str]) -> list[dict[str, float]]:
        self.seen += texts
        out = []
        for t in texts:
            if "jumps" in t:
                out.append({"positive": 0.9, "negative": 0.05, "neutral": 0.05})
            elif "penalty" in t or "plunges" in t:
                out.append({"positive": 0.02, "negative": 0.9, "neutral": 0.08})
            else:
                out.append({"positive": 0.1, "negative": 0.1, "neutral": 0.8})
        return out


# --- input construction ------------------------------------------------------------


def test_input_is_title_plus_sentences_mentioning_the_stock() -> None:
    body = (
        "Markets were volatile on Monday. Infosys won a large AI contract in Europe. "
        "Oil prices rose sharply. Analysts expect Infosys to raise guidance."
    )
    text = sentiment.build_input(
        "IT stocks in focus", None, body, entities.StockMatcher(INFY), dt.date(2026, 9, 20), 6
    )
    assert text == (
        "IT stocks in focus Infosys won a large AI contract in Europe. "
        "Analysts expect Infosys to raise guidance."
    )
    assert "Oil" not in text


def test_input_respects_max_sentences() -> None:
    body = " ".join(f"Infosys point number {i}." for i in range(10))
    text = sentiment.build_input(
        "Infosys news", None, body, entities.StockMatcher(INFY), dt.date(2026, 9, 20), 2
    )
    assert text == "Infosys news Infosys point number 0. Infosys point number 1."


# --- trading sessions --------------------------------------------------------------

# Mon 21 Sep 2026 .. Fri 25 Sep; Thu 24 Sep is a holiday (no price bar).
CALENDAR = sentiment.TradingCalendar([dt.date(2026, 9, d) for d in (18, 21, 22, 23, 25)])


def ist(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(
    ("moment", "session"),
    [
        (ist(21, 9, 0), dt.date(2026, 9, 21)),  # pre-open: same day
        (ist(21, 15, 30), dt.date(2026, 9, 21)),  # at the close: same day
        (ist(21, 15, 31), dt.date(2026, 9, 22)),  # after the close: next day
        (ist(19, 11, 0), dt.date(2026, 9, 21)),  # Saturday: Monday
        (ist(18, 20, 0), dt.date(2026, 9, 21)),  # Friday evening: Monday
        (ist(23, 16, 0), dt.date(2026, 9, 25)),  # before a holiday: skips it
        (ist(24, 10, 0), dt.date(2026, 9, 25)),  # on the holiday: next trading day
        (ist(25, 18, 0), dt.date(2026, 9, 28)),  # beyond stored prices: next weekday
    ],
)
def test_session_assignment(moment: dt.datetime, session: dt.date) -> None:
    assert sentiment.session_for(moment, CALENDAR) == session


def test_session_uses_ist_not_utc() -> None:
    # 10:30 UTC is 16:00 IST: after the close even though it's mid-day in UTC.
    moment = dt.datetime(2026, 9, 21, 10, 30, tzinfo=dt.UTC)
    assert sentiment.session_for(moment, CALENDAR) == dt.date(2026, 9, 22)


def test_news_time_ignores_published_dates_after_we_saw_it() -> None:
    seen = dt.datetime(2026, 9, 21, 5, tzinfo=dt.UTC)
    assert sentiment.news_time(seen + dt.timedelta(days=3), seen) == seen
    assert sentiment.news_time(None, seen) == seen
    assert sentiment.news_time(seen - dt.timedelta(hours=2), seen) == seen - dt.timedelta(hours=2)


# --- aggregation -------------------------------------------------------------------


def scored(*rows: tuple[str, str, str | None, float, float, dt.datetime]) -> pd.DataFrame:
    """(article_id, symbol, story_id, score, confidence, published_at)."""
    return pd.DataFrame(
        [
            {
                "article_id": a,
                "symbol": sym,
                "story_id": story,
                "score": score,
                "confidence": conf,
                "published_at": pub,
                "first_seen_at": pub + dt.timedelta(minutes=30),
            }
            for a, sym, story, score, conf, pub in rows
        ]
    )


def aggregate(df: pd.DataFrame) -> dict[tuple[str, dt.date], dict]:
    rows = sentiment.aggregate_daily(df, CALENDAR, -0.5, "fake/model")
    return {(r["symbol"], r["session_date"]): r for r in rows}


def test_copies_of_one_story_count_once() -> None:
    t = ist(21, 10)
    daily = aggregate(
        scored(
            ("a1", "INFY", "s1", -0.8, 0.95, t),
            ("a2", "INFY", "s1", -0.6, 0.95, t + dt.timedelta(hours=1)),
            ("a3", "INFY", "s1", -0.7, 0.8, t + dt.timedelta(hours=2)),
            ("b1", "INFY", "s2", 0.4, 0.95, t),
        )
    )
    row = daily[("INFY", dt.date(2026, 9, 21))]
    assert (row["story_count"], row["article_count"]) == (2, 4)
    assert row["mean_score"] == pytest.approx((-0.7 + 0.4) / 2)  # story means, not copies
    assert row["strong_negative_stories"] == 1


def test_weighted_score_uses_mention_confidence() -> None:
    t = ist(21, 10)
    row = aggregate(scored(("a", "INFY", "s1", 1.0, 0.9, t), ("b", "INFY", "s2", -1.0, 0.3, t)))[
        ("INFY", dt.date(2026, 9, 21))
    ]
    assert row["mean_score"] == pytest.approx(0.0)
    assert row["weighted_score"] == pytest.approx((0.9 - 0.3) / 1.2)


def test_story_belongs_to_session_of_its_earliest_copy() -> None:
    daily = aggregate(
        scored(
            ("a", "INFY", "s1", 0.5, 0.9, ist(21, 15)), ("b", "INFY", "s1", 0.5, 0.9, ist(21, 17))
        )
    )
    assert list(daily) == [("INFY", dt.date(2026, 9, 21))]
    assert daily[("INFY", dt.date(2026, 9, 21))]["article_count"] == 2


def test_after_close_news_goes_to_next_session_and_stocks_stay_separate() -> None:
    daily = aggregate(
        scored(
            ("a", "INFY", None, 0.5, 0.9, ist(21, 16)), ("b", "TCS", None, -0.5, 0.9, ist(21, 10))
        )
    )
    assert set(daily) == {("INFY", dt.date(2026, 9, 22)), ("TCS", dt.date(2026, 9, 21))}


# --- end to end with the database --------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def add_article(aid: str, title: str, published: dt.datetime, story: str | None = None) -> None:
    db.insert_new_articles(
        [
            {
                "id": aid,
                "url": f"https://x.com/{aid}",
                "title": title,
                "published_at": published,
                "first_seen_at": published + dt.timedelta(minutes=10),
                "fetched_via": "test",
            }
        ]
    )
    if story:
        db.set_story_ids({aid: story})


def test_score_skip_rescore_and_aggregate(engine: Engine) -> None:
    stocks = [INFY, TCS]
    add_article("a1", "Infosys profit jumps 40%", ist(21, 10), story="s1")
    add_article("a2", "Infosys profit jumps 40% on deal wins", ist(21, 11), story="s1")
    add_article("a3", "SEBI penalty on TCS for disclosure lapse", ist(21, 17))
    add_article("a4", "Sensex, Nifty end flat; Infosys, TCS among gainers", ist(21, 16))
    entities.run(stocks)

    scorer = FakeScorer()
    assert sentiment.score_pending(CONFIG, stocks, scorer) == 3  # wrap mentions not linked
    assert sentiment.score_pending(CONFIG, stocks, scorer) == 0  # already scored: skipped
    assert len(scorer.seen) == 3

    sentiment.rebuild_daily(CONFIG)
    daily = db.read_news_daily("fake/model").set_index(["symbol", "session_date"])
    infy = daily.loc[("INFY", dt.date(2026, 9, 21))]
    tcs = daily.loc[("TCS", dt.date(2026, 9, 22))]  # 17:00 IST -> next session
    assert (infy.story_count, infy.article_count) == (1, 2)
    assert infy.mean_score > 0.8
    assert (tcs.story_count, tcs.strong_negative_stories) == (1, 1)

    stored = db.read_scored_mentions("fake/model")
    assert set(stored["label"]) == {"positive", "negative"}
    assert (stored["score"] == stored["score"].clip(-1, 1)).all()


def test_relinking_an_article_drops_its_sentiment(engine: Engine) -> None:
    add_article("a1", "Infosys profit jumps 40%", ist(21, 10))
    entities.run([INFY])
    sentiment.score_pending(CONFIG, [INFY], FakeScorer())
    db.update_article_text("a1", "ok", 1, text="Infosys said revenue grew. " * 5)
    entities.run([INFY])
    assert db.read_scored_mentions("fake/model").empty
    assert sentiment.score_pending(CONFIG, [INFY], FakeScorer()) == 1


def test_models_are_stored_side_by_side(engine: Engine) -> None:
    add_article("a1", "Infosys profit jumps 40%", ist(21, 10))
    entities.run([INFY])
    other = FakeScorer()
    other.model_name = "other/model"
    sentiment.score_pending(CONFIG, [INFY], FakeScorer())
    sentiment.score_pending(CONFIG, [INFY], other)
    assert len(db.read_scored_mentions("fake/model")) == 1
    assert len(db.read_scored_mentions("other/model")) == 1


# --- optional: the real model ------------------------------------------------------


def finbert_cached() -> bool:
    """True if the pinned FinBERT revision is in the local Hugging Face cache."""
    try:
        from huggingface_hub import try_to_load_from_cache

        from config.news_sources import load_news_sources
    except ImportError:
        return False
    revision = load_news_sources().sentiment_revision
    path = try_to_load_from_cache("ProsusAI/finbert", "config.json", revision=revision)
    return isinstance(path, str)


@pytest.mark.skipif(not finbert_cached(), reason="FinBERT not downloaded")
def test_real_finbert_on_obvious_headlines() -> None:
    from config.news_sources import load_news_sources

    config = load_news_sources()
    scorer = sentiment.FinbertScorer("ProsusAI/finbert", config.sentiment_revision, 8)
    pos, neg = scorer.score(
        [
            "Infosys Q2 net profit jumps 40% on strong deal wins",
            "SEBI imposes penalty on HDFC Bank for regulatory lapses",
        ]
    )
    assert max(pos, key=pos.get) == "positive"
    assert max(neg, key=neg.get) == "negative"
    assert scorer.model_version == config.sentiment_revision
