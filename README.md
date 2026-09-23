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
  200-day SMA. These always use the full history.
- **Price chart:** candlesticks with SMA 50/200 and Bollinger Bands, plus volume.
- **RSI** (with 30/70 levels) and **MACD** (with histogram) panels.

Data is cached for 5 minutes. Use **Reload from database** in the sidebar after running an
update.

## Configuration

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
│   └── loader.py          # Loads and validates the watchlist
├── collectors/            # Fetch and store RAW data only (no analysis)
│   └── prices.py          # Daily OHLCV from Yahoo Finance
├── processing/            # Turn raw data into insight (no network calls)
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

**Database tables** (both keyed on `symbol, date`):

- `prices`: open, high, low, close, adj_close, volume, fetched_at (UTC)
- `indicators`: rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, sma_200,
  ema_20, bb_upper, bb_middle, bb_lower, atr_14, volume_sma_20

Architecture rules and coding conventions are in [`CLAUDE.md`](CLAUDE.md).

## Development

```bash
uv run pytest              # run all tests
uv run ruff check .        # lint
uv run ruff format .       # format
```

## Known limitations

- **Corporate actions:** prices are stored exactly as Yahoo provides them. Yahoo's
  `adj_close` did not adjust for the Tata Motors demerger (TMPV drops ~40% on
  2025-10-14), so TMPV's 52-week high and its indicators around that date reflect the
  demerger, not real trading.
- **Yahoo Finance** is an unofficial data source. Holiday filler bars (zero volume, flat
  price) are filtered out, but occasional gaps or revisions are possible.
- Indicators use unadjusted OHLC, as charting platforms do.

## Roadmap

1. ✅ Prices + technical indicators
2. News + sentiment
3. NSE/BSE filings (quarterly results)
4. Social media signals
5. Signals & backtesting
6. Scheduling, daily digests, Telegram alerts
