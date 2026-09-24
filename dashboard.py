"""Streamlit dashboard: price chart, indicators, news and social sentiment, metrics per stock.

Run with:  uv run streamlit run dashboard.py
"""

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from config.loader import Stock, load_watchlist
from config.news_sources import load_news_sources
from processing.adjustments import adjust_prices
from processing.entities import LINK_THRESHOLD
from processing.results import changes, joined_notes
from processing.sentiment import TradingCalendar, news_time, session_for
from storage.db import (
    project_path,
    read_corporate_actions,
    read_indicators,
    read_news_daily,
    read_pending_actions,
    read_prices,
    read_results,
    read_stock_filings,
    read_stock_news,
    read_story_sources,
    trading_dates,
)
from storage.social import read_linked_posts, read_social_daily

IST = ZoneInfo("Asia/Kolkata")
CACHE_TTL_S = 300
DEFAULT_RANGE_DAYS = 365
RSI_OVERBOUGHT, RSI_OVERSOLD = 70, 30
ADJUSTED, RAW = "Adjusted", "Raw"

UP_COLOR, DOWN_COLOR = "#26a69a", "#ef5350"
SMA50_COLOR, SMA200_COLOR, BB_COLOR = "#f5a623", "#7b61ff", "rgba(120,144,156,0.8)"
ACTION_COLOR = "#9e9e9e"
SENTIMENT_COLOR, STORIES_COLOR = "#ab47bc", "rgba(171,71,188,0.25)"
RESULTS_COLOR = "#26a69a"
REVENUE_COLOR, PROFIT_COLOR = "#5c6bc0", "#26a69a"
FILING_BADGE_COLORS = {
    "results": "green", "corporate_action": "orange", "dividend": "blue",
    "board_meeting": "violet", "credit_rating": "gray", "shareholding_pattern": "gray",
    "press_release": "blue", "insider_trading": "gray", "analyst_meet": "gray", "other": "gray",
}  # fmt: skip
# pending_actions statuses that mean "you may need to add this to corporate_actions.yaml"
PENDING_WARN = ("upcoming", "undated", "needs_review")
BADGE_THRESHOLD = 0.25  # story score at or beyond +/- this gets a positive/negative badge
MAX_NEWS_ITEMS = 40
MAX_SOCIAL_ITEMS = 40
PLATFORM_NAMES = {"valuepickr": "ValuePickr"}


@dataclass(frozen=True)
class Metrics:
    """Headline numbers for the latest trading day."""

    as_of: dt.date
    last_close: float
    prev_close: float | None
    day_change_pct: float | None
    high_52w: float
    low_52w: float
    rsi: float | None
    sma_200: float | None

    @property
    def above_sma_200(self) -> bool | None:
        """True/False if price is above/below SMA-200, None if SMA-200 isn't available."""
        return None if self.sma_200 is None else self.last_close > self.sma_200


# --- data -------------------------------------------------------------------------


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_watchlist() -> list[Stock]:
    """Watchlist entries, cached."""
    return load_watchlist()


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Loading prices…")
def cached_prices(symbol: str) -> pd.DataFrame:
    """All stored prices for `symbol`, cached."""
    return read_prices(symbol)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Loading indicators…")
def cached_indicators(symbol: str) -> pd.DataFrame:
    """All stored indicators for `symbol`, cached."""
    return read_indicators(symbol)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_actions(symbol: str) -> pd.DataFrame:
    """Corporate actions for `symbol`, cached."""
    return read_corporate_actions(symbol)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_news_daily(symbol: str, model_name: str) -> pd.DataFrame:
    """news_daily rows for `symbol`, cached."""
    daily = read_news_daily(model_name)
    return daily[daily["symbol"] == symbol] if not daily.empty else daily


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Loading news…")
def cached_news(symbol: str, model_name: str) -> pd.DataFrame:
    """One row per story linked to `symbol` (see news_items), cached."""
    articles = read_stock_news(symbol, model_name, LINK_THRESHOLD)
    story_ids = articles["story_id"].dropna().unique().tolist()
    return news_items(articles, read_story_sources(story_ids), TradingCalendar(trading_dates()))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_social_daily(symbol: str, model_name: str) -> pd.DataFrame:
    """social_daily rows for `symbol`, cached."""
    return read_social_daily(model_name, symbol)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Loading social posts…")
def cached_social_posts(symbol: str, model_name: str) -> pd.DataFrame:
    """Posts linked to `symbol` (links and scores only, no text), cached."""
    posts = read_linked_posts(model_name, LINK_THRESHOLD, symbol)
    return social_items(posts, TradingCalendar(trading_dates()))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_filings(symbol: str) -> pd.DataFrame:
    """This stock's filings, newest first, cached."""
    return read_stock_filings(symbol)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_results(symbol: str) -> pd.DataFrame:
    """This stock's results rows, cached."""
    results = read_results()
    return results[results["symbol"] == symbol]


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def cached_pending(symbol: str) -> pd.DataFrame:
    """This stock's announced corporate actions that may need adding to the YAML."""
    pending = read_pending_actions()
    if pending.empty:
        return pending
    return pending[(pending["symbol"] == symbol) & pending["status"].isin(PENDING_WARN)]


def filing_date(filed_at: pd.Series) -> pd.Series:
    """IST calendar date of each filing timestamp."""
    return pd.to_datetime(filed_at, utc=True).dt.tz_convert(IST).dt.date


def results_markers(filings: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """date, label for results filings in [start, end] (one per date), for chart markers."""
    if filings.empty:
        return pd.DataFrame(columns=["date", "label"])
    rows = filings[filings["filing_type"] == "results"].copy()
    rows["date"] = filing_date(rows["filed_at"])
    rows = rows[(rows["date"] >= start) & (rows["date"] <= end)]
    rows["label"] = rows["subject"].str.extract(r"(FY\d{2}Q\d)", expand=False).fillna("Results")
    approx = rows["subject"].str.contains("[date approx.]", regex=False).fillna(False)
    rows["label"] = rows["label"] + " results" + approx.map({True: " (date approx.)", False: ""})
    return rows.drop_duplicates("date")[["date", "label"]].reset_index(drop=True)


def filter_filings(
    filings: pd.DataFrame, types: list[str], start: dt.date, end: dt.date
) -> pd.DataFrame:
    """Filings of the given types filed in [start, end], newest first."""
    if filings.empty:
        return filings
    dates = filing_date(filings["filed_at"])
    mask = (dates >= start) & (dates <= end) & filings["filing_type"].fillna("other").isin(types)
    return filings[mask]


def filing_badge(filing_type: str | None) -> str:
    """Streamlit markdown badge for one of our filing types."""
    kind = filing_type or "other"
    return f":{FILING_BADGE_COLORS.get(kind, 'gray')}-badge[{kind.replace('_', ' ')}]"


def flags_text(raw: pd.DataFrame) -> str:
    """One quarter's validation flags; reviewed ones show as "reviewed: <reason>"."""
    flagged = raw[raw["flag"].notna()]
    reviewed = flagged["flag_reviewed"] if "flag_reviewed" in flagged else pd.Series(dtype=str)
    texts = {
        f"reviewed: {reviewed[i]}" if pd.notna(reviewed.get(i)) else flagged.at[i, "flag"]
        for i in flagged.index
    }
    return "; ".join(sorted(texts))


def results_table(results: pd.DataFrame, basis: str, quarters: int = 8) -> pd.DataFrame:
    """Last `quarters` of headline results for one basis, with QoQ/YoY.

    Columns: quarter, period_end, top_line (revenue, or total income when there's no
    revenue line, as for banks), top_line_yoy, top_line_qoq, net_profit, net_profit_yoy,
    net_profit_qoq, eps, source, flags, notes (why QoQ/YoY aren't like-for-like, if so).
    """
    rows = results[results["basis"] == basis]
    if rows.empty:
        return pd.DataFrame()
    diff = changes(rows)
    top = "revenue" if (diff["metric"] == "revenue").any() else "total_income"
    out = []
    for (quarter, period_end), q in diff.groupby(["fiscal_quarter", "period_end"]):
        by = q.set_index("metric")
        raw = rows[pd.to_datetime(rows["period_end"]).dt.date == period_end]
        row = {"quarter": quarter, "period_end": period_end, "top_line_metric": top}
        for metric, name in ((top, "top_line"), ("net_profit", "net_profit")):
            row[name] = by["value"].get(metric)
            row[f"{name}_yoy"] = by["yoy"].get(metric)
            row[f"{name}_qoq"] = by["qoq"].get(metric)
        row["eps"] = by["value"].get("eps")
        row["source"] = "PDF (lower trust)" if (raw["trust"] == "low").any() else "XBRL"
        row["flags"] = flags_text(raw)
        row["notes"] = joined_notes(q[q["metric"].isin([top, "net_profit"])])
        out.append(row)
    table = pd.DataFrame(out).sort_values("period_end")
    return table.tail(quarters).reset_index(drop=True)


def build_results_figure(table: pd.DataFrame) -> go.Figure:
    """Grouped bars of the top line and net profit per quarter, with YoY % lines."""
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    label = "Revenue" if table["top_line_metric"].iloc[0] == "revenue" else "Total income"
    for column, name, color in (("top_line", label, REVENUE_COLOR),
                                ("net_profit", "Net profit", PROFIT_COLOR)):  # fmt: skip
        fig.add_trace(
            go.Bar(x=table["quarter"], y=table[column], name=f"{name} (₹ cr)", marker_color=color)
        )
        fig.add_trace(
            go.Scatter(
                x=table["quarter"],
                y=table[f"{column}_yoy"] * 100,
                name=f"{name} YoY %",
                mode="lines+markers",
                line={"color": color, "dash": "dot"},
            ),
            secondary_y=True,
        )
    fig.update_yaxes(title_text="₹ crore", secondary_y=False)
    fig.update_yaxes(title_text="YoY %", secondary_y=True, showgrid=False)
    fig.update_layout(
        barmode="group",
        height=420,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    return fig


def news_items(
    articles: pd.DataFrame, story_sources: pd.DataFrame, calendar: TradingCalendar
) -> pd.DataFrame:
    """Collapse linked articles into one row per story, newest first.

    The earliest copy represents the story. `score` is the mean over scored copies,
    `more_sources` the number of other outlets that carried it, and `session_date` the
    IST trading session it belongs to.
    """
    if articles.empty:
        return articles.assign(news_time=[], session_date=[], more_sources=[])
    df = articles.copy()
    df["story"] = df["story_id"].fillna(df["article_id"])
    df["news_time"] = [
        news_time(p, f) for p, f in zip(df["published_at"], df["first_seen_at"], strict=True)
    ]
    df = df.sort_values("news_time")
    reps = df.groupby("story", sort=False).first()
    reps["score"] = df.groupby("story")["score"].mean()
    reps["session_date"] = [session_for(t, calendar) for t in reps["news_time"]]

    sources = pd.concat(
        [story_sources.rename(columns={"story_id": "story"}), df[["story", "source"]]]
    ).dropna()
    outlets = sources.groupby("story")["source"].agg(lambda s: set(s))
    reps["more_sources"] = [
        len(outlets.get(story, set()) - {src})
        for story, src in zip(reps.index, reps["source"], strict=True)
    ]
    return reps.reset_index().sort_values("news_time", ascending=False).reset_index(drop=True)


def social_items(posts: pd.DataFrame, calendar: TradingCalendar) -> pd.DataFrame:
    """Linked posts, newest first, with `post_time` (IST-aware) and `session_date`."""
    if posts.empty:
        return posts.assign(post_time=[], session_date=[])
    df = posts.copy()
    df["post_time"] = [
        news_time(c, f) for c, f in zip(df["created_at"], df["first_seen_at"], strict=True)
    ]
    df["session_date"] = [session_for(t, calendar) for t in df["post_time"]]
    return df.sort_values("post_time", ascending=False).reset_index(drop=True)


def social_summary(items: pd.DataFrame, end: dt.date, days: int) -> dict[str, float | None]:
    """Posts, distinct authors and confidence-weighted score over `days` sessions to `end`."""
    if items.empty:
        return {"posts": 0, "authors": 0, "score": None}
    sessions = pd.Series(items["session_date"])
    rows = items[(sessions > end - dt.timedelta(days=days)) & (sessions <= end)]
    scored = rows[rows["score"].notna()]
    score = (
        float((scored["score"] * scored["confidence"]).sum() / scored["confidence"].sum())
        if not scored.empty
        else None
    )
    return {"posts": len(rows), "authors": int(rows["author_hmac"].nunique()), "score": score}


def post_label(item: pd.Series) -> str:
    """Link text for a post: platform, topic title and post number (never post text)."""
    platform = PLATFORM_NAMES.get(item["platform"], item["platform"])
    topic = (
        item["topic_title"] if isinstance(item["topic_title"], str) else f"topic {item['topic_id']}"
    )
    number = f" #{int(item['post_number'])}" if pd.notna(item["post_number"]) else ""
    return f"{platform} · {topic}{number}"


def post_badge(item: dict) -> str:
    """Badge for a social post: its sentiment, or why it isn't scored."""
    kind = item.get("post_kind") or "opinion"
    if kind == "share":
        return ":gray-badge[shared link/headline]"
    if kind == "short":
        return ":gray-badge[short reply]"
    return sentiment_badge(item["score"])


def sentiment_badge(score: float | None) -> str:
    """Streamlit markdown badge for a story score."""
    if score is None or pd.isna(score):
        return ":gray-badge[unscored]"
    if score >= BADGE_THRESHOLD:
        return f":green-badge[positive {score:+.2f}]"
    if score <= -BADGE_THRESHOLD:
        return f":red-badge[negative {score:+.2f}]"
    return f":gray-badge[neutral {score:+.2f}]"


def escape_markdown(text: str) -> str:
    """Escape characters Streamlit markdown would interpret ($ is LaTeX, [] are links)."""
    for char in "\\$[]*_`~":
        text = text.replace(char, "\\" + char)
    return text


def sentiment_averages(daily: pd.DataFrame, end: dt.date) -> tuple[float | None, float | None]:
    """Story-weighted mean of daily weighted scores over the 7 and 30 days up to `end`."""

    def window(days: int) -> float | None:
        dates = pd.to_datetime(daily["session_date"]).dt.date
        rows = daily[(dates > end - dt.timedelta(days=days)) & (dates <= end)]
        if rows.empty or rows["story_count"].sum() == 0:
            return None
        return float(
            (rows["weighted_score"] * rows["story_count"]).sum() / rows["story_count"].sum()
        )

    if daily.empty:
        return None, None
    return window(7), window(30)


def actions_in_range(actions: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Corporate actions whose ex_date falls within [start, end]."""
    dates = actions["ex_date"].dt.date
    return actions[(dates >= start) & (dates <= end)]


def describe_action(action: pd.Series) -> str:
    """Short human label, e.g. 'Demerger 14 Oct 2025 (×0.6054)'."""
    return (
        f"{action['action_type'].capitalize()} {action['ex_date']:%d %b %Y} "
        f"(×{action['price_factor']:.4f})"
    )


def merge_prices_indicators(prices: pd.DataFrame, indicators: pd.DataFrame) -> pd.DataFrame:
    """Left-join indicators onto prices by date, sorted, with date as a column."""
    ind = indicators.drop(columns=["symbol"], errors="ignore")
    return prices.merge(ind, on="date", how="left").sort_values("date").reset_index(drop=True)


def compute_metrics(df: pd.DataFrame) -> Metrics:
    """Compute headline metrics from the full merged history (not the chart's date range)."""
    df = df.sort_values("date")
    last = df.iloc[-1]
    prev_close = df["close"].iloc[-2] if len(df) > 1 else None
    window = df[df["date"] > last["date"] - pd.DateOffset(weeks=52)]

    def optional(column: str) -> float | None:
        value = last.get(column)
        return None if value is None or pd.isna(value) else float(value)

    return Metrics(
        as_of=last["date"].date(),
        last_close=float(last["close"]),
        prev_close=None if prev_close is None else float(prev_close),
        day_change_pct=(None if not prev_close else (last["close"] / prev_close - 1) * 100),
        high_52w=float(window["high"].max()),
        low_52w=float(window["low"].min()),
        rsi=optional("rsi_14"),
        sma_200=optional("sma_200"),
    )


def today_ist() -> dt.date:
    """Today's date in IST."""
    return dt.datetime.now(IST).date()


def filter_range(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Rows with start <= date <= end."""
    dates = df["date"].dt.date
    return df[(dates >= start) & (dates <= end)]


def missing_trading_days(dates: pd.Series) -> list[str]:
    """Weekdays inside the range with no bar (exchange holidays), for hiding on the x-axis."""
    if dates.empty:
        return []
    all_weekdays = pd.bdate_range(dates.min(), dates.max())
    return all_weekdays.difference(pd.DatetimeIndex(dates)).strftime("%Y-%m-%d").tolist()


# --- chart ------------------------------------------------------------------------


def build_figure(
    df: pd.DataFrame,
    symbol: str,
    actions: pd.DataFrame | None = None,
    news: pd.DataFrame | None = None,
    results: pd.DataFrame | None = None,
) -> go.Figure:
    """Candlestick + SMA/Bollinger overlays, volume, RSI, MACD and (optionally) a news
    sentiment panel, all on one shared date axis.

    Each row of `actions` is drawn as a dashed vertical line labelled at the top. `news`
    is news_daily rows (session_date, weighted_score, story_count). `results` (date,
    label, from results_markers) adds a dotted line on the price panel per results date.
    """
    has = {c: c in df.columns and df[c].notna().any() for c in df.columns}
    with_news = news is not None
    titles = [f"{symbol} price", "Volume", "RSI (14)", "MACD (12, 26, 9)"]
    heights = [0.5, 0.12, 0.19, 0.19]
    if with_news:
        titles.append("News sentiment (line) and stories (bars)")
        heights = [0.42, 0.1, 0.15, 0.15, 0.18]
    fig = make_subplots(
        rows=len(titles),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=heights,
        subplot_titles=titles,
        specs=[[{"secondary_y": i == 4}] for i in range(len(titles))],
    )
    x = df["date"]

    # Bollinger bands first so the band fill sits behind the candles.
    if has.get("bb_upper") and has.get("bb_lower"):
        band = {"color": BB_COLOR, "width": 1, "dash": "dot"}
        fig.add_trace(go.Scatter(x=x, y=df["bb_upper"], name="BB upper", line=band), 1, 1)
        fig.add_trace(
            go.Scatter(
                x=x,
                y=df["bb_lower"],
                name="BB lower",
                line=band,
                fill="tonexty",
                fillcolor="rgba(120,144,156,0.08)",
            ),
            1,
            1,
        )
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="Price",
            increasing_line_color=UP_COLOR,
            decreasing_line_color=DOWN_COLOR,
        ),
        1,
        1,
    )
    for column, label, color in (
        ("sma_50", "SMA 50", SMA50_COLOR),
        ("sma_200", "SMA 200", SMA200_COLOR),
    ):
        if has.get(column):
            fig.add_trace(
                go.Scatter(x=x, y=df[column], name=label, line={"color": color, "width": 1.5}), 1, 1
            )

    up = df["close"] >= df["open"]
    fig.add_trace(
        go.Bar(
            x=x,
            y=df["volume"],
            name="Volume",
            marker_color=up.map({True: UP_COLOR, False: DOWN_COLOR}),
            showlegend=False,
        ),
        2,
        1,
    )

    if has.get("rsi_14"):
        fig.add_trace(
            go.Scatter(
                x=x, y=df["rsi_14"], name="RSI", line={"color": "#29b6f6"}, showlegend=False
            ),
            3,
            1,
        )
        for level in (RSI_OVERBOUGHT, RSI_OVERSOLD):
            fig.add_hline(y=level, line={"color": "grey", "dash": "dash", "width": 1}, row=3, col=1)
        fig.update_yaxes(range=[0, 100], row=3, col=1)

    if has.get("macd"):
        hist = df["macd_hist"]
        fig.add_trace(
            go.Bar(
                x=x,
                y=hist,
                name="MACD hist",
                marker_color=(hist >= 0).map({True: UP_COLOR, False: DOWN_COLOR}),
                showlegend=False,
            ),
            4,
            1,
        )
        fig.add_trace(go.Scatter(x=x, y=df["macd"], name="MACD", line={"color": "#29b6f6"}), 4, 1)
        fig.add_trace(
            go.Scatter(x=x, y=df["macd_signal"], name="Signal", line={"color": SMA50_COLOR}), 4, 1
        )

    if with_news:
        news_x = pd.to_datetime(news["session_date"])
        fig.add_trace(
            go.Bar(
                x=news_x,
                y=news["story_count"],
                name="Stories",
                marker_color=STORIES_COLOR,
                showlegend=False,
            ),
            5,
            1,
            secondary_y=True,
        )
        fig.add_trace(
            go.Scatter(
                x=news_x,
                y=news["weighted_score"],
                name="Sentiment",
                mode="lines+markers",
                line={"color": SENTIMENT_COLOR},
                showlegend=False,
            ),
            5,
            1,
        )
        fig.add_hline(y=0, line={"color": "grey", "dash": "dot", "width": 1}, row=5, col=1)
        fig.update_yaxes(range=[-1, 1], title_text="score", row=5, col=1)
        fig.update_yaxes(title_text="stories", showgrid=False, row=5, col=1, secondary_y=True)

    for _, action in (actions if actions is not None else pd.DataFrame()).iterrows():
        # A paper-referenced shape draws one continuous line through all panels.
        fig.add_shape(
            type="line",
            x0=action["ex_date"],
            x1=action["ex_date"],
            xref="x",
            y0=0,
            y1=1,
            yref="paper",
            line={"color": ACTION_COLOR, "dash": "dash", "width": 1},
        )
        fig.add_annotation(
            x=action["ex_date"],
            xref="x",
            y=1,
            yref="paper",
            text=describe_action(action),
            showarrow=False,
            xanchor="left",
            yanchor="top",
            font={"size": 11, "color": ACTION_COLOR},
            bgcolor="rgba(0,0,0,0)",
        )

    for marker in (results if results is not None else pd.DataFrame()).itertuples():
        fig.add_shape(
            type="line",
            x0=marker.date,
            x1=marker.date,
            xref="x",
            y0=0,
            y1=1,
            yref="y domain",
            line={"color": RESULTS_COLOR, "dash": "dot", "width": 1},
        )
        fig.add_annotation(
            x=marker.date,
            xref="x",
            y=0,
            yref="y domain",
            text=marker.label,
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            font={"size": 10, "color": RESULTS_COLOR},
        )

    fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}, {"values": missing_trading_days(x)}])
    fig.update_layout(
        height=1150 if with_news else 950,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    return fig


# --- UI ---------------------------------------------------------------------------


def render_metrics(m: Metrics, sentiment: tuple[float | None, float | None]) -> None:
    """Render the top row of metric cards; `sentiment` is (7-day, 30-day) averages."""
    cols = st.columns(7)
    cols[0].metric("Last close", f"₹{m.last_close:,.2f}", border=True)
    cols[1].metric(
        "Day change",
        "—" if m.prev_close is None else f"₹{m.last_close - m.prev_close:+,.2f}",
        delta=None if m.day_change_pct is None else f"{m.day_change_pct:+.2f}%",
        border=True,
    )
    cols[2].metric("52-week high", f"₹{m.high_52w:,.2f}", border=True)
    cols[3].metric("52-week low", f"₹{m.low_52w:,.2f}", border=True)

    if m.rsi is None:
        rsi_note = None
    elif m.rsi >= RSI_OVERBOUGHT:
        rsi_note = "overbought"
    elif m.rsi <= RSI_OVERSOLD:
        rsi_note = "oversold"
    else:
        rsi_note = "neutral"
    cols[4].metric(
        "RSI (14)",
        "—" if m.rsi is None else f"{m.rsi:.1f}",
        delta=rsi_note,
        delta_color="off",
        border=True,
    )

    if m.above_sma_200 is None:
        cols[5].metric("vs 200-day SMA", "—", help="Needs 200 days of history", border=True)
    else:
        gap = (m.last_close / m.sma_200 - 1) * 100
        cols[5].metric(
            "vs 200-day SMA",
            "Above" if m.above_sma_200 else "Below",
            delta=f"{gap:+.1f}%",
            border=True,
        )

    avg7, avg30 = sentiment
    cols[6].metric(
        "News sentiment 7d",
        "—" if avg7 is None else f"{avg7:+.2f}",
        delta=None if avg7 is None or avg30 is None else f"{avg7 - avg30:+.2f} vs 30d",
        help="Story-weighted FinBERT score (-1 to +1) over the last 7 days, compared "
        f"with the 30-day average ({'—' if avg30 is None else f'{avg30:+.2f}'}).",
        border=True,
    )


def render_news_list(items: pd.DataFrame, start: dt.date, end: dt.date) -> None:
    """News stories for the selected range: linked headline, source, IST time, badge."""
    st.subheader("News")
    in_range = (
        items[(items["session_date"] >= start) & (items["session_date"] <= end)]
        if not items.empty
        else items
    )
    if in_range.empty:
        st.caption(
            "No linked news in this date range. Collect it with `uv run python run_update.py`."
        )
        return
    for item in in_range.head(MAX_NEWS_ITEMS).itertuples():
        when = pd.Timestamp(item.news_time).tz_convert(IST)
        more = (
            f" · +{item.more_sources} more source{'s' if item.more_sources > 1 else ''}"
            if item.more_sources
            else ""
        )
        st.markdown(
            f"[{escape_markdown(item.title)}]({item.url})  \n"
            f"{escape_markdown(item.source or 'unknown')} · {when:%d %b, %H:%M} IST{more} "
            f"· {sentiment_badge(item.score)}"
        )
    if len(in_range) > MAX_NEWS_ITEMS:
        st.caption(f"Showing the latest {MAX_NEWS_ITEMS} of {len(in_range)} stories.")


def render_social(
    items: pd.DataFrame, daily: pd.DataFrame, start: dt.date, end: dt.date, today: dt.date
) -> None:
    """Social activity: counts, sentiment and links to posts. Post text is never shown."""
    st.subheader("Social")
    if items.empty:
        st.caption(
            "No linked social posts yet. ValuePickr posts are collected by "
            "`uv run python run_update.py` once the topics in config/social_sources.yaml "
            "are confirmed."
        )
        return
    week, month = social_summary(items, today, 7), social_summary(items, today, 30)
    cols = st.columns(4)
    cols[0].metric("Posts 7d", week["posts"], delta=f"{month['posts']} in 30d", delta_color="off",
                   border=True)  # fmt: skip
    cols[1].metric("Authors 7d", week["authors"], delta=f"{month['authors']} in 30d",
                   delta_color="off", border=True)  # fmt: skip
    for col, label, value in ((cols[2], "Sentiment 7d", week["score"]),
                              (cols[3], "Sentiment 30d", month["score"])):  # fmt: skip
        col.metric(label, "—" if value is None else f"{value:+.2f}", border=True,
                   help="Confidence-weighted FinBERT score (-1 to +1) of the sentences "
                   "about this stock.")  # fmt: skip

    if not daily.empty:
        sessions = pd.to_datetime(daily["session_date"])
        view = daily[(sessions.dt.date >= start) & (sessions.dt.date <= end)]
        if not view.empty:
            chart = pd.DataFrame(
                {"posts": view["post_count"].values, "authors": view["author_count"].values},
                index=pd.to_datetime(view["session_date"]),
            )
            st.bar_chart(chart, stack=False)

    in_range = items[(items["session_date"] >= start) & (items["session_date"] <= end)]
    for item in in_range.head(MAX_SOCIAL_ITEMS).to_dict("records"):
        when = pd.Timestamp(item["post_time"]).tz_convert(IST)
        how = "thread" if item["method"] == "thread" else f"mentioned ({item['confidence']:.2f})"
        st.markdown(
            f"[{escape_markdown(post_label(pd.Series(item)))}]({item['url']})  \n"
            f"{when:%d %b %Y, %H:%M} IST · {how} · {post_badge(item)}"
        )
    if len(in_range) > MAX_SOCIAL_ITEMS:
        st.caption(f"Showing the latest {MAX_SOCIAL_ITEMS} of {len(in_range)} posts.")
    st.caption(
        "Post text isn't shown: links open the post on ValuePickr. ValuePickr content is "
        "CC BY-NC-SA 3.0 and is stored for personal, non-commercial analysis only."
    )


def render_pending_actions(pending: pd.DataFrame) -> None:
    """Warning banner for announced corporate actions not yet in corporate_actions.yaml."""
    for a in pending.itertuples():
        when = f"ex-date {a.ex_date:%d %b %Y}" if pd.notna(a.ex_date) else "no ex-date found"
        ratio = f" {a.ratio}" if a.ratio else ""
        st.warning(
            f"**Announced {a.action_type}{ratio} ({when}) is not in "
            f"`config/corporate_actions.yaml`** [{a.status}]. {a.note}",
            icon="⚠️",
        )


def render_filings(filings: pd.DataFrame, start: dt.date, end: dt.date) -> None:
    """Filings for the selected range, with a category filter."""
    if filings.empty:
        st.caption(
            "No filings stored for this stock. Results files come from the inbox "
            "(`uv run python -m collectors.result_files checklist`) or its IR site."
        )
        return
    present = sorted(filings["filing_type"].fillna("other").unique())
    types = st.multiselect("Categories", present, default=present, key="filing_types")
    shown = filter_filings(filings, types, start, end)
    if shown.empty:
        st.caption("No filings of these categories in the selected range.")
        return
    for f in shown.itertuples():
        when = pd.Timestamp(f.filed_at).tz_convert(IST)
        cols = st.columns([5, 1])
        cols[0].markdown(
            f"{when:%d %b %Y} · {filing_badge(f.filing_type)} · "
            f"{escape_markdown(f.subject or f.category or '')}  \n"
            f"{escape_markdown(f.category or '')} · {f.exchange}"
        )
        if isinstance(f.attachment_url, str) and f.attachment_url:
            cols[1].link_button("Open", f.attachment_url)
        elif isinstance(f.attachment_path, str) and project_path(f.attachment_path).exists():
            path = project_path(f.attachment_path)
            cols[1].download_button(
                "Download", path.read_bytes(), file_name=path.name, key=f"dl-{f.id}"
            )


def render_results(results: pd.DataFrame) -> None:
    """Quarterly revenue/profit bars with YoY lines and the last-8-quarters table."""
    if results.empty:
        st.caption(
            "No results for this stock yet. See which quarters to download with "
            "`uv run python -m collectors.result_files checklist`."
        )
        return
    bases = [b for b in ("consolidated", "standalone") if (results["basis"] == b).any()]
    basis = st.radio("Basis", bases, horizontal=True, key="results_basis")
    table = results_table(results, basis)
    st.plotly_chart(build_results_figure(table), width="stretch")
    shown = table.drop(columns=["top_line_metric"]).rename(
        columns={
            "top_line": "revenue"
            if table["top_line_metric"].iloc[0] == "revenue"
            else "total income"
        }  # fmt: skip
    )
    pct = [c for c in shown.columns if c.endswith(("_yoy", "_qoq"))]
    st.dataframe(
        shown.style.format({**dict.fromkeys(pct, "{:+.1%}"), "eps": "{:.2f}"}, na_rep="—").format(
            precision=0,
            thousands=",",
            subset=[c for c in shown.columns if c in ("revenue", "total income", "net_profit")],
        ),  # fmt: skip
        hide_index=True,
        width="stretch",
    )
    st.caption("₹ crore; EPS in ₹ per share, as reported (not restated for bonuses/splits).")
    for quarter, note in table.loc[table["notes"] != "", ["quarter", "notes"]].itertuples(
        index=False
    ):
        st.caption(f"⚠ {quarter}: {note}")


def sidebar(stocks: list[Stock]) -> tuple[Stock, str]:
    """Render the stock selector, candle mode toggle and reload button."""
    st.sidebar.title("stock-intel")
    by_symbol = {s.symbol: s for s in stocks}
    symbol = st.sidebar.selectbox(
        "Stock",
        list(by_symbol),
        format_func=lambda s: f"{s} · {by_symbol[s].name}",
    )
    mode = st.sidebar.radio(
        "Candles",
        [ADJUSTED, RAW],
        horizontal=True,
        help="Adjusted prices remove jumps from splits, bonuses and demergers recorded in "
        "config/corporate_actions.yaml. Raw prices are exactly as Yahoo returned them.",
    )
    if st.sidebar.button("Reload from database", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    return by_symbol[symbol], mode


def date_range_picker(first: dt.date, last: dt.date) -> tuple[dt.date, dt.date]:
    """Sidebar date range picker defaulting to the last year of data."""
    default_start = max(first, last - dt.timedelta(days=DEFAULT_RANGE_DAYS))
    picked = st.sidebar.date_input(
        "Date range",
        value=(default_start, last),
        min_value=first,
        max_value=last,
        format="DD/MM/YYYY",
    )
    # While the user is mid-selection Streamlit returns a single date.
    if isinstance(picked, tuple) and len(picked) == 2:
        return picked[0], picked[1]
    start = picked[0] if isinstance(picked, tuple) and picked else default_start
    return start, last


def main() -> None:
    """Render the dashboard."""
    st.set_page_config(page_title="stock-intel", page_icon="📈", layout="wide")
    stock, mode = sidebar(cached_watchlist())

    prices = cached_prices(stock.symbol)
    st.header(f"{stock.name} ({stock.symbol})")
    st.caption(f"{stock.sector} · Yahoo: {stock.yf}")
    render_pending_actions(cached_pending(stock.symbol))

    if prices.empty:
        st.info(
            f"No price data for **{stock.symbol}** yet. Collect it with:\n\n"
            "```\nuv run python -m collectors.prices\n```"
        )
        return

    indicators = cached_indicators(stock.symbol)
    if indicators.empty:
        st.warning(
            "Indicators haven't been computed for this stock yet, so overlays, RSI and MACD "
            "are hidden. Run `uv run python -m processing.indicators`."
        )
    actions = cached_actions(stock.symbol)
    adjusted = merge_prices_indicators(adjust_prices(prices, actions), indicators)

    model_name = load_news_sources().sentiment_model
    news_daily = cached_news_daily(stock.symbol, model_name)
    last_price_date = adjusted["date"].max().date()

    # Metrics always use adjusted prices so 52-week ranges and SMA gaps are comparable.
    render_metrics(
        compute_metrics(adjusted), sentiment_averages(news_daily, max(last_price_date, today_ist()))
    )

    df = adjusted if mode == ADJUSTED else merge_prices_indicators(prices, indicators)
    first, last = df["date"].min().date(), df["date"].max().date()
    start, end = date_range_picker(first, last)
    view = filter_range(df, start, end)
    if view.empty:
        st.info("No trading days in the selected range. Try widening it.")
        return
    visible_actions = actions_in_range(actions, start, end)
    news_view = news_daily
    if not news_daily.empty:
        sessions = pd.to_datetime(news_daily["session_date"]).dt.date
        news_view = news_daily[(sessions >= start) & (sessions <= end)]
    filings = cached_filings(stock.symbol)
    markers = results_markers(filings, start, end)
    st.plotly_chart(
        build_figure(view, stock.symbol, visible_actions, news_view, markers), width="stretch"
    )

    for _, action in visible_actions.iterrows():
        effect = (
            "prices before this date are scaled so the chart is continuous"
            if mode == ADJUSTED
            else "raw candles show the jump; indicator overlays are still computed from "
            "adjusted prices, so they won't line up with candles before this date"
        )
        st.info(
            f"**{describe_action(action)}** — {effect}. Source: {action['source']}.",
            icon="ℹ️",
        )

    news_tab, social_tab, filings_tab, results_tab = st.tabs(
        ["News", "Social", "Filings", "Results"]
    )
    with news_tab:
        render_news_list(cached_news(stock.symbol, model_name), start, end)
    with social_tab:
        render_social(
            cached_social_posts(stock.symbol, model_name),
            cached_social_daily(stock.symbol, model_name),
            start,
            end,
            max(last_price_date, today_ist()),
        )
    with filings_tab:
        render_filings(filings, start, end)
    with results_tab:
        render_results(cached_results(stock.symbol))

    fetched = prices["fetched_at"].max().astimezone(IST)
    st.caption(
        f"Data through {last:%d %b %Y} · last fetched {fetched:%d %b %Y, %H:%M} IST · "
        f"metrics use full adjusted history; the chart shows {mode.lower()} prices "
        "for the selected range."
    )


if __name__ == "__main__":
    main()
