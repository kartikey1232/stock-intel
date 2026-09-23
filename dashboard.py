"""Streamlit dashboard: price chart, indicators and key metrics per watchlist stock.

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
from storage.db import read_indicators, read_prices

IST = ZoneInfo("Asia/Kolkata")
CACHE_TTL_S = 300
DEFAULT_RANGE_DAYS = 365
RSI_OVERBOUGHT, RSI_OVERSOLD = 70, 30

UP_COLOR, DOWN_COLOR = "#26a69a", "#ef5350"
SMA50_COLOR, SMA200_COLOR, BB_COLOR = "#f5a623", "#7b61ff", "rgba(120,144,156,0.8)"


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


def build_figure(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Candlestick + SMA/Bollinger overlays, volume, RSI and MACD in one shared-x figure."""
    has = {c: c in df.columns and df[c].notna().any() for c in df.columns}
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.5, 0.12, 0.19, 0.19],
        subplot_titles=(f"{symbol} price", "Volume", "RSI (14)", "MACD (12, 26, 9)"),
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

    fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}, {"values": missing_trading_days(x)}])
    fig.update_layout(
        height=950,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    return fig


# --- UI ---------------------------------------------------------------------------


def render_metrics(m: Metrics) -> None:
    """Render the top row of metric cards."""
    cols = st.columns(6)
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


def sidebar(stocks: list[Stock]) -> Stock:
    """Render the stock selector and reload button; return the chosen stock."""
    st.sidebar.title("stock-intel")
    by_symbol = {s.symbol: s for s in stocks}
    symbol = st.sidebar.selectbox(
        "Stock",
        list(by_symbol),
        format_func=lambda s: f"{s} · {by_symbol[s].name}",
    )
    if st.sidebar.button("Reload from database", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    return by_symbol[symbol]


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
    stock = sidebar(cached_watchlist())

    prices = cached_prices(stock.symbol)
    st.header(f"{stock.name} ({stock.symbol})")
    st.caption(f"{stock.sector} · Yahoo: {stock.yf}")

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
    df = merge_prices_indicators(prices, indicators)

    render_metrics(compute_metrics(df))

    first, last = df["date"].min().date(), df["date"].max().date()
    start, end = date_range_picker(first, last)
    view = filter_range(df, start, end)
    if view.empty:
        st.info("No trading days in the selected range. Try widening it.")
        return
    st.plotly_chart(build_figure(view, stock.symbol), width="stretch")

    fetched = prices["fetched_at"].max().astimezone(IST)
    st.caption(
        f"Data through {last:%d %b %Y} · last fetched {fetched:%d %b %Y, %H:%M} IST · "
        "metrics use the full history; the chart uses the selected range."
    )


if __name__ == "__main__":
    main()
