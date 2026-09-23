import datetime as dt

import numpy as np
import pandas as pd
import pytest

import dashboard
from processing.adjustments import adjust_prices
from processing.indicators import compute_indicators


def history(days: int = 300, last_close: float = 110.0) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-09-23", periods=days)
    close = np.linspace(100, last_close, days)
    prices = pd.DataFrame(
        {
            "symbol": "TEST",
            "date": dates,
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 1000,
        }
    )
    return dashboard.merge_prices_indicators(prices, compute_indicators(prices))


def test_metrics_use_latest_day_and_52_week_window() -> None:
    df = history()
    df.loc[0, "high"] = 999.0  # > 52 weeks before the last date: must be ignored
    m = dashboard.compute_metrics(df)

    assert m.as_of == dt.date(2026, 9, 23)
    assert m.last_close == pytest.approx(110.0)
    expected_change = (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
    assert m.day_change_pct == pytest.approx(expected_change)
    assert m.high_52w == pytest.approx(112.0)
    assert m.rsi == pytest.approx(df["rsi_14"].iloc[-1])
    assert m.above_sma_200 is True


def test_metrics_below_sma_200() -> None:
    df = history()
    df.loc[df.index[-1], "close"] = 50.0
    assert dashboard.compute_metrics(df).above_sma_200 is False


def test_metrics_handle_missing_indicators() -> None:
    df = history(days=30)
    m = dashboard.compute_metrics(df)
    assert m.sma_200 is None and m.above_sma_200 is None


def test_filter_range_is_inclusive() -> None:
    df = history(days=10)
    out = dashboard.filter_range(df, dt.date(2026, 9, 21), dt.date(2026, 9, 23))
    assert out["date"].dt.date.tolist() == [dt.date(2026, 9, d) for d in (21, 22, 23)]


def test_missing_trading_days_finds_weekday_holidays() -> None:
    dates = pd.Series(pd.to_datetime(["2026-04-30", "2026-05-04"]))  # 1 May holiday, weekend
    assert dashboard.missing_trading_days(dates) == ["2026-05-01"]


def test_build_figure_with_and_without_indicators() -> None:
    df = history()
    full = dashboard.build_figure(df, "TEST")
    names = {t.name for t in full.data}
    assert {"Price", "SMA 50", "SMA 200", "BB upper", "BB lower", "RSI", "MACD"} <= names

    prices_only = df[["symbol", "date", "open", "high", "low", "close", "volume"]]
    bare = dashboard.build_figure(prices_only, "TEST")
    assert {t.name for t in bare.data} == {"Price", "Volume"}


def actions_df(ex_date: str = "2026-09-01", factor: float = 0.6) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["TEST"],
            "ex_date": [pd.Timestamp(ex_date)],
            "action_type": ["demerger"],
            "price_factor": [factor],
            "source": ["test"],
            "note": [None],
        }
    )


def test_actions_in_range_filters_by_ex_date() -> None:
    actions = actions_df()
    assert len(dashboard.actions_in_range(actions, dt.date(2026, 8, 1), dt.date(2026, 9, 1))) == 1
    assert dashboard.actions_in_range(actions, dt.date(2026, 9, 2), dt.date(2026, 9, 30)).empty


def test_figure_marks_corporate_actions() -> None:
    fig = dashboard.build_figure(history(), "TEST", actions_df())
    labels = [a.text for a in fig.layout.annotations]
    assert "Demerger 01 Sep 2026 (×0.6000)" in labels
    assert any(s.type == "line" and s.yref == "paper" for s in fig.layout.shapes)


def test_metrics_on_adjusted_prices_remove_the_demerger_high() -> None:
    df = history(days=300, last_close=110)
    df.loc[df["date"] < pd.Timestamp("2026-09-01"), ["open", "high", "low", "close"]] *= 2
    raw_high = dashboard.compute_metrics(df).high_52w
    adjusted = adjust_prices(df, actions_df(factor=0.5))
    assert dashboard.compute_metrics(adjusted).high_52w < raw_high / 1.8


# --- news --------------------------------------------------------------------------

IST = dashboard.IST
NEWS_CALENDAR = dashboard.TradingCalendar([dt.date(2026, 9, d) for d in (18, 21, 22, 23)])


def linked_articles(*rows) -> pd.DataFrame:
    """(article_id, story_id, source, IST hour on 21 Sep, score)."""
    return pd.DataFrame(
        [
            {
                "article_id": aid,
                "title": f"Headline {aid}",
                "url": f"https://x.com/{aid}",
                "source": source,
                "story_id": story,
                "published_at": dt.datetime(2026, 9, 21, hour, tzinfo=IST),
                "first_seen_at": dt.datetime(2026, 9, 21, hour, 30, tzinfo=IST),
                "confidence": 0.95,
                "label": None,
                "score": score,
            }
            for aid, story, source, hour, score in rows
        ]
    )


def test_news_items_collapse_stories_and_count_other_sources() -> None:
    articles = linked_articles(
        ("a1", "s1", "Mint", 10, 0.6),
        ("a2", "s1", "ET", 11, 0.2),
        ("b1", None, "Upstox", 17, -0.5),
    )
    sources = pd.DataFrame(
        {"story_id": ["s1", "s1", "s1"], "source": ["Mint", "ET", "NDTV Profit"]}
    )  # NDTV carried it too but isn't linked to this stock
    items = dashboard.news_items(articles, sources, NEWS_CALENDAR)

    assert items["article_id"].tolist() == ["b1", "a1"]  # newest first, earliest copy shown
    story = items.set_index("article_id").loc["a1"]
    assert story["more_sources"] == 2
    assert story["score"] == pytest.approx(0.4)
    assert story["session_date"] == dt.date(2026, 9, 21)
    after_close = items.set_index("article_id").loc["b1"]
    assert (after_close["more_sources"], after_close["session_date"]) == (0, dt.date(2026, 9, 22))


def test_news_items_handles_no_news() -> None:
    empty = linked_articles().reindex(
        columns=["article_id", "story_id", "source", "published_at", "first_seen_at", "score"]
    )
    assert dashboard.news_items(
        empty, pd.DataFrame(columns=["story_id", "source"]), NEWS_CALENDAR
    ).empty


@pytest.mark.parametrize(
    ("score", "badge"),
    [(0.6, "green-badge"), (-0.4, "red-badge"), (0.1, "gray-badge[neutral"), (None, "unscored")],
)
def test_sentiment_badge(score, badge: str) -> None:
    assert badge in dashboard.sentiment_badge(score)


def test_escape_markdown_neutralises_links_and_latex() -> None:
    assert dashboard.escape_markdown("Q2 [update] $5bn") == "Q2 \\[update\\] \\$5bn"


def test_sentiment_averages_are_story_weighted() -> None:
    daily = pd.DataFrame(
        {
            "session_date": [dt.date(2026, 9, 1), dt.date(2026, 9, 21), dt.date(2026, 9, 22)],
            "weighted_score": [-0.8, 0.5, -0.1],
            "story_count": [4, 3, 1],
        }
    )
    avg7, avg30 = dashboard.sentiment_averages(daily, dt.date(2026, 9, 23))
    assert avg7 == pytest.approx((0.5 * 3 - 0.1) / 4)
    assert avg30 == pytest.approx((-0.8 * 4 + 0.5 * 3 - 0.1) / 8)
    assert dashboard.sentiment_averages(daily.iloc[0:0], dt.date(2026, 9, 23)) == (None, None)


def test_figure_adds_news_panel_on_shared_axis() -> None:
    news = pd.DataFrame(
        {"session_date": [dt.date(2026, 9, 21)], "weighted_score": [0.3], "story_count": [4]}
    )
    fig = dashboard.build_figure(history(), "TEST", news=news)
    names = {t.name for t in fig.data}
    assert {"Stories", "Sentiment"} <= names
    sentiment = next(t for t in fig.data if t.name == "Sentiment")
    assert sentiment.xaxis == "x5"
    # shared_xaxes links every other panel's x-axis to the bottom (news) one
    assert [fig.layout[f"xaxis{i}"].matches for i in ("", 2, 3, 4)] == ["x5"] * 4


# --- filings and results ------------------------------------------------------------


def filings_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": ["a", "b", "c", "d"],
            "exchange": ["IR", "IR", "NSE", "NSE"],
            "filed_at": pd.to_datetime(
                ["2026-07-17 18:30", "2022-08-13 18:30", "2026-07-20 05:00", "2026-07-17 20:00"],
                utc=True,
            ),
            "category": ["Financial Results"] * 2 + ["Board Meeting", "Financial Results"],
            "subject": [
                "FY27Q1 consolidated+standalone results (pdf)",
                "FY23Q1 consolidated+standalone results (pdf) [date approx.]",
                "Board meeting to consider dividend",
                "FY27Q1 standalone results (xbrl)",
            ],
            "filing_type": ["results", "results", "board_meeting", "results"],
            "attachment_url": ["https://x/a.pdf", None, None, None],
            "attachment_path": [None, None, None, None],
        }
    )


def test_results_markers_use_ist_dates_and_flag_approximate_ones() -> None:
    m = dashboard.results_markers(filings_df(), dt.date(2022, 1, 1), dt.date(2026, 9, 23))
    assert m.to_dict("records") == [
        {"date": dt.date(2026, 7, 18), "label": "FY27Q1 results"},  # 18:30 UTC = midnight IST
        {"date": dt.date(2022, 8, 14), "label": "FY23Q1 results (date approx.)"},
    ]  # two filings on 18 Jul collapse into one marker
    assert dashboard.results_markers(filings_df(), dt.date(2026, 8, 1), dt.date(2026, 9, 23)).empty


def test_filter_filings_by_type_and_range() -> None:
    shown = dashboard.filter_filings(
        filings_df(), ["board_meeting"], dt.date(2026, 7, 1), dt.date(2026, 9, 23)
    )
    assert shown["id"].tolist() == ["c"]
    both = dashboard.filter_filings(
        filings_df(), ["results", "board_meeting"], dt.date(2026, 7, 1), dt.date(2026, 9, 23)
    )
    assert set(both["id"]) == {"a", "c", "d"}


def test_filing_badge() -> None:
    assert dashboard.filing_badge("corporate_action") == ":orange-badge[corporate action]"
    assert dashboard.filing_badge(None) == ":gray-badge[other]"


def result_rows(metric_values: dict[str, list[float]], trust: str = "high") -> pd.DataFrame:
    ends = pd.to_datetime(["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
    rows = []
    for metric, values in metric_values.items():
        for end, value in zip(ends, values, strict=True):
            rows.append({"symbol": "X", "period_end": end.date(), "basis": "consolidated",
                         "metric": metric, "fiscal_quarter": "", "value": value, "unit": "",
                         "source": "xbrl", "trust": trust, "flag": None})  # fmt: skip
    df = pd.DataFrame(rows)
    from processing.results import fiscal_quarter

    df["fiscal_quarter"] = df["period_end"].map(fiscal_quarter)
    return df


def test_results_table_has_yoy_qoq_and_falls_back_to_total_income() -> None:
    rows = result_rows(
        {"total_income": [100, 110, 120, 130, 150], "net_profit": [10, 11, 12, 13, 20]}
    )
    table = dashboard.results_table(rows, "consolidated")
    last = table.iloc[-1]
    assert (last["quarter"], last["top_line_metric"]) == ("FY27Q1", "total_income")
    assert last["top_line_yoy"] == pytest.approx(0.5)
    assert last["net_profit_qoq"] == pytest.approx(20 / 13 - 1)
    assert last["source"] == "XBRL"
    assert dashboard.results_table(rows, "standalone").empty


def test_results_figure_has_bars_and_yoy_lines() -> None:
    table = dashboard.results_table(
        result_rows(
            {"revenue": [100, 110, 120, 130, 150], "net_profit": [10, 11, 12, 13, 20]}, trust="low"
        ),  # fmt: skip
        "consolidated",
    )
    assert table["source"].iloc[-1] == "PDF (lower trust)"
    names = [t.name for t in dashboard.build_results_figure(table).data]
    assert names == ["Revenue (₹ cr)", "Revenue YoY %", "Net profit (₹ cr)", "Net profit YoY %"]


def test_price_figure_draws_results_markers() -> None:
    markers = pd.DataFrame({"date": [dt.date(2026, 7, 18)], "label": ["FY27Q1 results"]})
    fig = dashboard.build_figure(history(), "TEST", results=markers)
    assert "FY27Q1 results" in [a.text for a in fig.layout.annotations]
