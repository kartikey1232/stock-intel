import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

import processing.stories as stories
from config.loader import Stock
from config.news_sources import NewsConfig
from storage import db

T0 = pd.Timestamp("2026-09-20 06:00", tz="UTC")
STOCKS = [
    Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys",)),
    Stock("HDFCBANK", "HDFCBANK.NS", "HDFC Bank Ltd", "Banks", ("HDFC Bank",)),
]
COMPANIES = stories.company_patterns(STOCKS)


def articles(*rows: tuple[str, str, float]) -> pd.DataFrame:
    """(id, title, hours after T0) -> frame shaped like read_articles_for_grouping()."""
    return pd.DataFrame(
        {
            "id": [r[0] for r in rows],
            "title": [r[1] for r in rows],
            "source": "test",
            "published_at": [T0 + pd.Timedelta(hours=r[2]) for r in rows],
            "first_seen_at": [T0 + pd.Timedelta(hours=r[2] + 1) for r in rows],
            "story_id": None,
        }
    )


def group(df: pd.DataFrame, threshold: float = 90) -> dict[str, str]:
    return stories.group_stories(df, threshold, window_hours=48, min_tokens=6, companies=COMPANIES)


def test_same_story_from_two_sources_is_grouped() -> None:
    result = group(
        articles(
            ("a", "Infosys expands Indore development centre with new 3.3 lakh sq ft block", 0),
            ("b", "Infosys Expands Indore Development Centre With New 3.3-Lakh Sq Ft Block", 5),
        )
    )
    assert result == {"a": "a", "b": "a"}


def test_title_extension_is_grouped() -> None:
    result = group(
        articles(
            ("a", "RIL's Jio-bp curbs diesel fills as West Asia war strains supplies", 0),
            ("b", "RIL's Jio-bp curbs diesel fills as West Asia war strains supplies - Mint", 2),
        )
    )
    assert result["b"] == "a"


def test_different_stories_about_same_company_stay_apart() -> None:
    result = group(
        articles(
            ("a", "Infosys shares jump 4% after winning large AI deal from European bank", 0),
            ("b", "Infosys expands Indore development centre with new software block", 3),
        )
    )
    assert result == {"a": "a", "b": "b"}


def test_templated_headlines_for_different_dates_and_companies_stay_apart() -> None:
    result = group(
        articles(
            ("a", "Infosys Share Price Prediction for Tomorrow: 18 Sep 2026", 0),
            ("b", "Infosys Share Price Prediction for Tomorrow: 21 Sep 2026", 20),
            ("c", "HDFC Bank Share Price Prediction for Tomorrow: 18 Sep 2026", 1),
            ("d", "Infosys falls Friday, underperforms market", 2),
            ("e", "Infosys falls Wednesday, underperforms market", 3),
        )
    )
    assert len(set(result.values())) == 5


def test_short_titles_use_strict_matching() -> None:
    result = group(
        articles(("a", "TCS Outlook for the Week", 0), ("b", "INFY Outlook for the Week", 1))
    )
    assert result["a"] != result["b"]


def test_articles_outside_window_are_separate_stories() -> None:
    title = "HDFC Bank CEO succession: three scenarios as RBI decision nears"
    result = group(articles(("a", title, 0), ("b", title, 49)))
    assert result == {"a": "a", "b": "b"}


def test_missing_published_at_falls_back_to_first_seen() -> None:
    df = articles(
        ("a", "Barc in talks with NTPC, Adani, RIL to deploy small reactors", 0),
        ("b", "Barc in talks with NTPC, Adani, RIL to deploy small reactors", 100),
    )
    df.loc[1, "published_at"] = None
    df.loc[1, "first_seen_at"] = T0 + pd.Timedelta(hours=2)
    assert group(df)["b"] == "a"


def test_threshold_is_configurable() -> None:
    df = articles(
        ("a", "Sebi to address derivatives expiry settlement price concerns says chief", 0),
        ("b", "Sebi to address concerns over settlement price for derivatives on expiry", 1),
    )
    assert group(df, threshold=95)["b"] == "b"
    assert group(df, threshold=70)["b"] == "a"


def test_existing_story_id_is_kept() -> None:
    df = articles(
        ("a", "Jaguar Land Rover recalls over 23,000 vehicles in US", 0),
        ("b", "Jaguar Land Rover recalls over 23,000 vehicles in US: see affected models", 1),
    )
    df.loc[1, "story_id"] = "old-story"
    assert group(df) == {"a": "old-story", "b": "old-story"}


# --- persistence -------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def test_run_stores_story_ids_and_rerun_changes_nothing(engine: Engine) -> None:
    rows = articles(
        ("a1", "HDFC Bank adds 93K crore to m-cap as stock gains in 7 of 8 sessions", 0),
        ("a2", "HDFC Bank adds 93K crore to m-cap as stock gains in 7 of 8 sessions", 4),
        ("a3", "Infosys shares jump 4% after winning large AI deal", 6),
    )
    db.insert_new_articles(
        [
            {**r, "url": f"https://x.com/{r['id']}", "fetched_via": "test"}
            for r in rows.drop(columns="story_id").to_dict("records")
        ]
    )
    config = NewsConfig("ua", 5, {}, "https://g", {}, "7d")

    changed = stories.run(config, STOCKS)
    assert changed == {"a1": "a1", "a2": "a1", "a3": "a3"}
    stored = db.read_articles_for_grouping().set_index("id")["story_id"].to_dict()
    assert stored == {"a1": "a1", "a2": "a1", "a3": "a3"}

    assert stories.run(config, STOCKS) == {}  # nothing ungrouped
    assert stories.run(config, STOCKS, full=True) == {}  # full regroup is stable


def test_ungrouped_time_is_utc(engine: Engine) -> None:
    db.insert_new_articles(
        [
            {
                "id": "x",
                "url": "https://x.com/x",
                "title": "t",
                "first_seen_at": dt.datetime(2026, 9, 20, 6, tzinfo=dt.UTC),
                "fetched_via": "test",
            }
        ]
    )
    assert db.earliest_ungrouped_time() == dt.datetime(2026, 9, 20, 6, tzinfo=dt.UTC)
