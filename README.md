# stock-intel

A personal stock intelligence tool for Indian markets (NSE/BSE). It collects daily prices
from Yahoo Finance, computes technical indicators, and shows them in a Streamlit dashboard.

**Current status:** Phase 1 (prices + indicators) is complete. News sentiment, exchange
filings, social signals, backtesting and Telegram alerts are planned. See [Roadmap](#roadmap).

## Setup

Requires macOS, Linux or WSL, and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install uv (skip if `uv --version` works)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install Python 3.12 and all dependencies into .venv
uv sync

# 3. Create your local config (no keys are needed for Phase 1)
cp .env.example .env
```

## Running the update

One command downloads new prices and recomputes indicators for every stock in the
watchlist:

```bash
uv run python run_update.py
```

- **First run** downloads 5 years of daily history (about 15 seconds for 5 stocks).
- **Later runs** fetch only from the last stored date onward, and indicators are
  recomputed incrementally.
- **Safe to re-run at any time.** Every write is an upsert, so nothing is duplicated.
  Today's bar is refreshed on each run, so run it after the market closes (3:30 pm IST)
  to store the final close.
- **One stock failing doesn't stop the others.** Failures are summarised at the end, and
  the exit code is `1` if anything failed, which is useful for schedulers.

Each step can also run on its own:

```bash
uv run python -m collectors.prices           # prices only
uv run python -m processing.indicators       # indicators only (incremental)
uv run python -m processing.indicators --full   # recompute all indicator history
uv run python run_update.py --full-indicators   # both, with a full indicator recompute
```

Logs go to the console and to `logs/stock_intel.log` (rotating, UTC timestamps).

## Dashboard

```bash
uv run streamlit run dashboard.py
```

Opens at <http://localhost:8501>. Pick a stock and date range in the sidebar. The page shows:

- **Metric cards:** last close, day change, 52-week high/low, RSI, and position vs the
  200-day SMA. These always use the full, corporate-action adjusted history.
- **Candles toggle:** adjusted (default) or raw. When a corporate action falls in the
  visible range, it is marked on the chart and explained below it.
- **Price chart:** candlesticks with SMA 50/200 and Bollinger Bands, plus volume.
- **RSI** (with 30/70 levels) and **MACD** (with histogram) panels.

Data is cached for 5 minutes. Use **Reload from database** in the sidebar after running an
update.

## Configuration

**Corporate actions:** [`config/corporate_actions.yaml`](config/corporate_actions.yaml)
records splits, bonuses and demergers that break price continuity. Raw prices are never
changed. Indicators, the dashboard's metric cards and its default "Adjusted" candles use
prices where everything before an ex-date is multiplied by the action's `price_factor`:

```yaml
- symbol: TMPV
  ex_date: 2025-10-14
  action_type: demerger        # split | bonus | demerger | other
  price_factor: 0.6053726825576996   # new base price / prior close (400.00 / 660.75)
  source: NSE special pre-open price discovery session, 14 Oct 2025
  note: Demerger of the commercial vehicle business (now TMCV).
```

For `split` and `bonus`, volume before the ex-date is divided by the factor. The file is
synced into the `corporate_actions` table on every update, and any stock whose actions
changed gets a full indicator recompute automatically.

The indicators step logs a **warning for any overnight gap over 25% with no recorded
action**. That is the signal to add an entry. **Only add actions that show up as a gap.**
Yahoo already back-adjusts most splits and bonuses (e.g. HDFC Bank's 1:1 bonus in Aug 2025
shows no gap), and recording one of those would adjust it twice.

**Watchlist:** edit [`config/watchlist.yaml`](config/watchlist.yaml). Each stock needs:

```yaml
- symbol: INFY            # NSE symbol (unique)
  yf: INFY.NS             # Yahoo ticker: .NS for NSE, .BO for BSE
  name: Infosys Ltd
  sector: Information Technology
  aliases: [Infosys, Infy]   # names used in news (for Phase 2 matching)
```

The file is validated on load. Missing, unknown or duplicate fields raise a clear error.
New stocks get their full 5-year history on the next update.

**Environment (`.env`):**

| Variable | Used for | Default |
|---|---|---|
| `DB_PATH` | SQLite database file (relative to project root) | `data/stock_intel.db` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts (Phase 6) | — |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Social signals (Phase 4) | — |
| `ANTHROPIC_API_KEY` | News sentiment (Phase 2) | — |

## Project structure

```
.
├── run_update.py          # Entry point: collect prices → compute indicators
├── dashboard.py           # Streamlit + Plotly dashboard
├── config/
│   ├── watchlist.yaml     # Stocks to track
│   ├── loader.py          # Loads and validates the watchlist
│   ├── corporate_actions.yaml  # Splits/bonuses/demergers (reviewed in git)
│   └── corporate_actions.py    # Loads and validates corporate actions
├── collectors/            # Fetch and store RAW data only (no analysis)
│   └── prices.py          # Daily OHLCV from Yahoo Finance
├── processing/            # Turn raw data into insight (no network calls)
│   ├── adjustments.py     # Adjusted OHLC + unrecorded-gap detection
│   └── indicators.py      # RSI, MACD, SMA, EMA, Bollinger, ATR via pandas-ta
├── storage/
│   └── db.py              # SQLAlchemy schema, upserts, reads
├── utils/
│   ├── logging_setup.py   # Console + rotating file logging
│   └── retry.py           # Exponential-backoff retry decorator
├── tests/                 # pytest suite (network calls are mocked)
├── data/                  # SQLite database (git-ignored)
└── logs/                  # Log files (git-ignored)
```

**Database tables:**

- `prices` (`symbol, date`): open, high, low, close, adj_close, volume, fetched_at (UTC).
  Stored exactly as Yahoo returned them.
- `indicators` (`symbol, date`): rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50,
  sma_200, ema_20, bb_upper, bb_middle, bb_lower, atr_14, volume_sma_20. Computed from
  adjusted prices.
- `corporate_actions` (`symbol, ex_date`): action_type, price_factor, source, note.
  A mirror of `config/corporate_actions.yaml`.

Architecture rules and coding conventions are in [`CLAUDE.md`](CLAUDE.md).

## Development

```bash
uv run pytest              # run all tests
uv run ruff check .        # lint
uv run ruff format .       # format
```

## Known limitations

- **Corporate actions are recorded by hand.** Unrecorded gaps over 25% are flagged in
  the logs, but smaller unadjusted actions (e.g. a demerger worth under 25% of the
  price) would not be caught. The TMPV demerger (Oct 2025) is recorded, using NSE's
  discovered base price.
- **Stored history can go stale** if Yahoo later back-adjusts a split. Old rows are not
  re-fetched, so the stored data keeps a gap that Yahoo's current data no longer has.
  The gap detector will flag it; record it as a split, or delete the database and
  re-collect.
- **Dividends are not adjusted**, apart from Yahoo's `adj_close`. Indicators use
  split/demerger-adjusted OHLC, as charting platforms do.
- **Yahoo Finance** is an unofficial data source. Holiday filler bars (zero volume, flat
  price) are filtered out, but occasional gaps or revisions are possible.

## Roadmap

1. ✅ Prices + technical indicators
2. News + sentiment
3. NSE/BSE filings (quarterly results)
4. Social media signals
5. Signals & backtesting
6. Scheduling, daily digests, Telegram alerts
