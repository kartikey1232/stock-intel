"""Tests for social linking, scoring and daily aggregation, and the social config loader."""

import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

import dashboard
from config.loader import load_watchlist
from config.news_sources import load_news_sources
from config.social_sources import (
    DEFAULT_SOCIAL_SOURCES_PATH,
    SocialSourcesError,
    Topic,
    ValuePickrConfig,
    load_social_sources,
)
from processing import social
from processing.entities import StockMatcher
from processing.sentiment import TradingCalendar
from storage import db
from storage.social import insert_new_posts, read_linked_posts, read_social_daily, upsert_topic

STOCKS = load_watchlist()
BY_SYMBOL = {s.symbol: s for s in STOCKS}
NEWS_CONFIG = load_news_sources()
CONFIG = ValuePickrConfig(
    base_url="https://forum.example.com",
    user_agent="test",
    topics=[
        Topic(24141, "HDFC Bank thread", "HDFCBANK"),
        Topic(1233, "Tata Motors thread", "TMPV", dt.date(2025, 10, 14)),
    ],
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DISCOVERED = 555  # an unconfigured topic, as found via /latest.json


class FakeScorer:
    """Keyword stand-in for FinBERT that records its inputs."""

    model_name = "fake/model"
    model_version = "abc123"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def score(self, texts: Sequence[str]) -> list[dict[str, float]]:
        self.seen += texts
        return [
            {"positive": 0.9, "negative": 0.05, "neutral": 0.05}
            if "strong" in t
            else {"positive": 0.05, "negative": 0.9, "neutral": 0.05}
            for t in texts
        ]


def links(topic_id: int, text: str, created: dt.date) -> dict[str, dict]:
    matchers = [StockMatcher(s) for s in STOCKS]
    mentions = social.link_post("valuepickr", str(topic_id), text, created, CONFIG, matchers)
    return {m["symbol"]: m for m in mentions}


# --- linking -----------------------------------------------------------------------


def test_dedicated_topic_posts_link_to_its_stock_without_the_linker() -> None:
    found = links(
        24141, "Credit costs look fine; ICICI Bank is cheaper though.", dt.date(2026, 9, 1)
    )
    assert found == {"HDFCBANK": {"symbol": "HDFCBANK", "method": "thread",
                                  "matched_alias": None, "confidence": 0.9}}  # fmt: skip


def test_tata_motors_thread_defaults_to_tmpv_only_before_the_demerger() -> None:
    before = links(1233, "Tata Motors order book is strong.", dt.date(2025, 10, 13))
    assert before["TMPV"]["method"] == "thread"
    # From the ex-date bare "Tata Motors" is the CV company unless the sentence is about PVs.
    assert links(1233, "Tata Motors truck volumes rose.", dt.date(2025, 10, 14)) == {}
    after = links(1233, "JLR margins improved; Tata Motors Nexon EV sales beat estimates.",
                  dt.date(2025, 10, 20))  # fmt: skip
    assert after["TMPV"]["method"] == "linker"
    assert after["TMPV"]["confidence"] >= 0.5


def test_own_thread_conditional_match_is_boosted_to_link() -> None:
    # Post-demerger PV sentence in TMPV's own thread: one conditional "Tata Motors" match
    # (0.45 on its own) is lifted to 0.6, so it links.
    pv = "Tata Motors Nexon EV sales beat estimates."
    own = links(1233, pv, dt.date(2025, 10, 20))
    assert own["TMPV"] == {"symbol": "TMPV", "method": "linker",
                           "matched_alias": "Tata Motors", "confidence": 0.6}  # fmt: skip
    # The same sentence elsewhere keeps the linker's score; the CV sense still never links.
    assert links(DISCOVERED, pv, dt.date(2025, 10, 20))["TMPV"]["confidence"] == (
        pytest.approx(0.45)
    )
    assert links(1233, "Tata Motors truck volumes rose.", dt.date(2025, 10, 20)) == {}
    # A strong-alias match isn't lowered or changed by the boost.
    jlr = links(1233, "JLR margins improved. JLR volumes too. JLR again.", dt.date(2025, 10, 20))
    assert jlr["TMPV"]["confidence"] == pytest.approx(0.7)


def test_discovered_topic_posts_go_through_the_entity_linker() -> None:
    found = links(
        DISCOVERED, "Infosys wins a large deal. Infosys guidance raised.", dt.date(2026, 9, 1)
    )
    assert set(found) == {"INFY"} and found["INFY"]["method"] == "linker"
    assert links(DISCOVERED, "Markets were flat today.", dt.date(2026, 9, 1)) == {}


# --- scoring input -----------------------------------------------------------------


def test_only_sentences_about_the_stock_are_scored() -> None:
    text = "Nifty fell 2% today.\nInfosys margins were strong. TCS disappointed."
    matcher = StockMatcher(BY_SYMBOL["INFY"])
    assert social.build_post_input(text, matcher, dt.date(2026, 9, 1), "linker", 6) == (
        "Infosys margins were strong."
    )


def test_thread_posts_that_never_name_the_stock_use_their_first_sentences() -> None:
    text = "Results were decent. NIMs compressed a bit. Deposits grew. Will hold."
    matcher = StockMatcher(BY_SYMBOL["HDFCBANK"])
    assert social.build_post_input(text, matcher, dt.date(2026, 9, 1), "thread", 2) == (
        "Results were decent. NIMs compressed a bit."
    )
    assert social.build_post_input(text, matcher, dt.date(2026, 9, 1), "linker", 2) == ""


# --- end to end over a tmp database ------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def add_post(pid: int, topic: int, text: str, created: dt.datetime, author: str) -> None:
    insert_new_posts([{
        "id": f"valuepickr:{pid}", "platform": "valuepickr", "topic_id": str(topic),
        "platform_post_id": str(pid), "post_number": pid, "url": f"https://f/t/x/{topic}/{pid}",
        "text": text, "author_hmac": author, "likes": 0, "created_at": created,
        "first_seen_at": dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC), "checked_at": created,
        "linked_at": None,
    }])  # fmt: skip


def test_link_score_and_aggregate(engine: Engine) -> None:
    upsert_topic({"platform": "valuepickr", "topic_id": "24141", "title": "HDFC Bank thread",
                  "role": "dedicated", "fetched_at": dt.datetime.now(dt.UTC)})  # fmt: skip
    monday = dt.datetime(2026, 9, 21, 10, tzinfo=IST)
    add_post(1, 24141, "Deposit growth is strong.", monday, "a")
    add_post(2, 24141, "Asset quality worries me.", monday + dt.timedelta(hours=1), "a")
    add_post(3, 24141, "A strong quarter overall.", monday + dt.timedelta(hours=2), "b")
    add_post(4, 24141, "Late post: strong numbers.", dt.datetime(2026, 9, 21, 17, tzinfo=IST), "c")
    add_post(5, DISCOVERED, "Infosys looks strong here.", monday, "d")
    add_post(6, DISCOVERED, "Nothing about our stocks.", monday, "e")

    assert social.link(STOCKS, CONFIG) == 6
    assert social.link(STOCKS, CONFIG) == 0  # already linked
    scorer = FakeScorer()
    assert social.score_pending(NEWS_CONFIG, STOCKS, scorer) == 5
    assert social.score_pending(NEWS_CONFIG, STOCKS, scorer) == 0

    calendar = TradingCalendar([dt.date(2026, 9, 21), dt.date(2026, 9, 22)])
    rows = social.aggregate_daily(read_linked_posts("fake/model", 0.5), calendar, "fake/model")
    daily = {(r["symbol"], r["session_date"]): r for r in rows}
    hdfc = daily[("HDFCBANK", dt.date(2026, 9, 21))]
    assert (hdfc["post_count"], hdfc["author_count"], hdfc["scored_count"]) == (3, 2, 3)
    assert hdfc["mean_score"] == pytest.approx((0.85 - 0.85 + 0.85) / 3)
    assert daily[("HDFCBANK", dt.date(2026, 9, 22))]["post_count"] == 1  # after 15:30 IST
    assert daily[("INFY", dt.date(2026, 9, 21))]["author_count"] == 1

    social.rebuild_daily(NEWS_CONFIG)  # real model name: nothing scored under it yet
    stored = read_social_daily(NEWS_CONFIG.sentiment_model)
    assert set(stored["symbol"]) == {"HDFCBANK", "INFY"}
    assert stored["weighted_score"].isna().all() and (stored["scored_count"] == 0).all()


def test_dashboard_items_have_links_and_scores_but_no_text(engine: Engine) -> None:
    upsert_topic({"platform": "valuepickr", "topic_id": "24141", "title": "HDFC Bank thread",
                  "role": "dedicated", "fetched_at": dt.datetime.now(dt.UTC)})  # fmt: skip
    add_post(1, 24141, "Secret post text strong.", dt.datetime(2026, 9, 21, 10, tzinfo=IST), "a")
    social.link(STOCKS, CONFIG)
    social.score_pending(NEWS_CONFIG, STOCKS, FakeScorer())
    posts = read_linked_posts("fake/model", 0.5, "HDFCBANK")
    assert "text" not in posts.columns
    items = dashboard.social_items(posts, TradingCalendar([dt.date(2026, 9, 21)]))
    item = items.iloc[0]
    assert dashboard.post_label(item) == "ValuePickr · HDFC Bank thread #1"
    assert item["session_date"] == dt.date(2026, 9, 21)
    summary = dashboard.social_summary(items, dt.date(2026, 9, 21), 7)
    assert summary == {"posts": 1, "authors": 1, "score": pytest.approx(0.85)}
    assert dashboard.social_summary(items.iloc[0:0], dt.date(2026, 9, 21), 7)["score"] is None


# --- config ------------------------------------------------------------------------


def test_project_social_config_is_valid_and_maps_topics() -> None:
    cfg = load_social_sources(DEFAULT_SOCIAL_SOURCES_PATH)
    symbols = {t.id: t.symbol for t in cfg.topics}
    assert symbols == {24141: "HDFCBANK", 32873: "RELIANCE", 8124: "INFY", 1233: "TMPV"}
    assert cfg.confirmed
    assert cfg.topic(1233).default_until == dt.date(2025, 10, 14)
    assert cfg.min_interval_s >= 5
    assert all(t.symbol in BY_SYMBOL for t in cfg.topics if t.symbol)


@pytest.mark.parametrize(
    ("topics", "message"),
    [
        ("- {id: 1, symbol: X}\n    - {id: 1}", "duplicate"),
        ("- {id: -3}", "positive integer"),
        ("- {id: 4, default_until: 2025-10-14}", "needs a symbol"),
        ("- {id: 5, colour: red}", "unknown"),
    ],
)
def test_invalid_topics_are_rejected(tmp_path: Path, topics: str, message: str) -> None:
    path = tmp_path / "social.yaml"
    path.write_text(
        "valuepickr:\n  base_url: https://f.example.com\n  user_agent: t\n"
        f"  topics:\n    {topics}\n",
        encoding="utf-8",
    )
    with pytest.raises(SocialSourcesError, match=message):
        load_social_sources(path)


def test_unconfirmed_is_the_default(tmp_path: Path) -> None:
    path = tmp_path / "social.yaml"
    path.write_text("valuepickr:\n  base_url: https://f.example.com\n  user_agent: t\n")
    assert load_social_sources(path).confirmed is False


def test_linked_posts_frame_has_expected_columns_when_empty(engine: Engine) -> None:
    empty = read_linked_posts("fake/model", 0.5)
    assert empty.empty and "url" in empty.columns
    assert dashboard.social_items(empty, TradingCalendar([])).empty
    assert isinstance(social.aggregate_daily(empty, TradingCalendar([]), "m"), list)
    assert pd.api.types.is_object_dtype(empty["url"]) or empty["url"].empty
