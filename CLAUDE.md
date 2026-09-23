# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in this repository.

## Project

**stock-intel** is a personal stock intelligence tool for Indian markets (NSE/BSE). It collects
prices, computes technical indicators, and will later add news sentiment, exchange filings
(quarterly results), social media signals, and rule-based alerts.

It is for personal use today, but structure everything so it can grow into a larger product:
clear module boundaries, no shortcuts that assume a single user or a single database.

## Tech stack

- Python 3.12
- **uv** for dependency and environment management (no pip/poetry)
- **SQLite via SQLAlchemy**. Keep the SQL portable so we can move to PostgreSQL later: no
  SQLite-only features and no raw SQL where the ORM or Core can express it.
- pandas, yfinance, Streamlit (UI), APScheduler (scheduling)
- pytest for tests

## Commands

```bash
uv sync                    # install dependencies
uv add <pkg>               # add a dependency (never edit pyproject by hand for deps)
uv run pytest              # run all tests
uv run pytest path/to/test_file.py::test_name   # run a single test
uv run ruff check . && uv run ruff format .     # lint + format
uv run python run_update.py                     # collect prices, then compute indicators
uv run python -m collectors.prices              # prices only
uv run python -m processing.indicators [--full] # indicators only
uv run streamlit run dashboard.py               # launch the dashboard
```

## Gotchas

- yfinance never raises on failure; it returns an empty frame. Collectors must decide
  what "empty" means (first run = error, incremental = no new data).
- Yahoo inserts filler bars on NSE holidays (zero volume, flat OHLC); the price collector
  drops them.
- Corporate actions: `prices` stays raw forever; adjustment happens in
  `processing/adjustments.py` from `config/corporate_actions.yaml` (source of truth,
  synced into the `corporate_actions` table). Indicators and dashboard metrics must use
  adjusted prices. Yahoo already back-adjusts most splits/bonuses, so only record an
  action when raw data shows an unadjusted gap (the indicators step warns on >25%
  overnight gaps), or history gets double-adjusted.
- pandas-ta 0.4 emits RSI from bar 2; `processing/indicators.py` masks the warm-up.
  pandas-ta is a beta release pinned in `uv.lock`, and it caps numpy at 2.2 via numba.
- Tests must never hit the network or the real database; use a tmp SQLite engine and
  monkeypatch `storage.db.get_engine`.

## Architecture principles (non-negotiable)

1. **Collectors fetch, processing interprets. Never mix the two.**
   - `collectors/` only fetches and stores **raw** data (prices, news, filings, posts).
   - `processing/` turns raw data into insight (indicators, sentiment, signals).
   - A collector must not compute indicators; a processor must not make network calls.
2. **All jobs are idempotent.** Re-running a job never creates duplicates. Use upserts keyed on
   natural keys (e.g. `(symbol, exchange, date)` for daily prices), not blind inserts.
3. **Every network call is defensive:** explicit timeouts, retries with exponential backoff, and
   rate limiting. Never call an external API without all three.
4. **Time:** store all timestamps in **UTC** (timezone-aware). Convert to **IST
   (`Asia/Kolkata`)** only at the display layer. Keep this in mind for NSE/BSE trading hours
   and market dates.
5. **Secrets** live only in `.env`, loaded via `python-dotenv`. Never hardcode keys or tokens,
   and never commit `.env` (keep a `.env.example` with placeholder values up to date).
6. **Logging:** use the `logging` module, never `print`. Logs are written to `logs/`.

## Code style

- Type hints on all functions.
- Small, single-purpose functions.
- Docstrings on all public functions.
- Tests with pytest for every new module. Mock network calls in tests; tests must not hit
  real APIs.

## Roadmap

| Phase | Scope |
|-------|-------|
| 1 | Prices + technical indicators |
| 2 | News + sentiment |
| 3 | NSE/BSE filings (quarterly results) |
| 4 | Social media signals |
| 5 | Signals & backtesting |
| 6 | Scheduling, digests, Telegram alerts |

Build only what the current phase needs, but don't make choices that block later phases.
