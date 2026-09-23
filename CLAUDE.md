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
uv run python run_update.py [--skip-news] [--skip-filings]  # prices -> indicators -> news -> filings
uv run python -m collectors.prices              # prices only
uv run python -m processing.indicators [--full] # indicators only
uv run python -m collectors.news                # news articles (Google News + publisher RSS)
uv run python -m collectors.article_text [--reclean]  # full text for pending articles
uv run python -m processing.stories [--full]    # group syndicated copies into stories
uv run python -m processing.entities [--full]   # link articles to watchlist stocks
uv run python -m processing.entities --evaluate # precision/recall on labelled headlines
uv run python -m processing.sentiment [--report] # FinBERT scores + news_daily aggregates
uv run python -m processing.filing_categories   # categorise filings, detect corporate actions
uv run python -m collectors.result_files import     # import results files from data/filings/inbox/
uv run python -m collectors.result_files checklist  # which quarters x bases are missing, and where from
uv run python -m collectors.results_ir          # HDFC Bank results PDFs from its IR site
uv run python -m processing.results --report    # rebuild results table; last 8 quarters
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
- **Google News links are stored unresolved.** `news.google.com/rss/articles/...` links
  are not HTTP redirects (they return a JS page), and decoding them needs Google's
  undocumented internal API, so it isn't reliable. Articles from Google keep the Google
  URL (with `oc` stripped); the publisher name comes from the entry's `<source>` element.
  Consequences: the same story can be stored twice (Google link + publisher-feed link),
  so cross-source de-duplication must use title + publisher, not URL; and full-text
  fetching can't use these URLs directly.
- News: `articles` rows are insert-only (first write wins), so `first_seen_at` is when we
  first had the article. Backtests must filter on `first_seen_at`, never `published_at`,
  which comes from the feed and can be wrong. `collectors/news.py` stores raw articles only;
  deciding which stock an article is about belongs in `processing/` (a Google query hit is
  not proof: ~80-95% of titles mention the company).
- Article full text is fetched in `collectors/article_text.py`, not `processing/`: page
  fetching is a network call, and extracted body text is still raw content. Status flow:
  pending -> ok | paywalled | skipped | failed, with `text_attempts` capping retries at
  `article_text.max_attempts`. Google News articles are always `skipped` (unresolvable
  links). Paywalls are detected only via schema.org `isAccessibleForFree: false` or HTTP
  402. The words "paywall"/"premium"/"prime" appear in every ET and Mint page's chrome.
- Site boilerplate that trafilatura keeps is stripped by per-domain line regexes in
  `config/news_sources.yaml` (`article_text.boilerplate`). This matters for entity matching:
  ET's footer lists "Top Trending Stocks: ... HDFC Bank ..., Infosys ...", and "Read more:" /
  "Also Read |" lines link to other stories. After changing rules, run
  `collectors.article_text --reclean`.
- Stories: count `story_id`s, not articles, when aggregating news (one wire story is often
  syndicated 2-4 times). Grouping = token-set ratio on normalised titles within 48h, but
  titles whose numbers, weekdays or named watchlist companies conflict never group
  (templated headlines like "<Company> Share Price Prediction for <date>" otherwise merge).
- Entity linking (`processing/entities.py`): an article is "about" a stock when its
  `article_mentions.confidence >= LINK_THRESHOLD` (0.5). Lower-confidence rows (passing
  mentions, stocks listed in market wraps) are stored on purpose; filter, don't delete.
  Name rules live in `config/watchlist.yaml` (`aliases`, `ambiguous_aliases`,
  `exclude_patterns`, `conditional_aliases`). After changing them, run
  `processing.entities --full` and check `tests/fixtures/labelled_headlines.yaml`. The
  test fails if precision or recall drops below 0.9. Add new real-world misses to that
  fixture instead of tuning rules to it.
- Linking case rules: all-caps names (RIL, TCS, and each stock's NSE symbol, which is
  always a strong alias) are case-sensitive. Other strong aliases ignore case. Ambiguous
  aliases and context terms must be Capitalised or ALL CAPS ("reliance" the word never
  matches). exclude_patterns are case-insensitive, so they also mask ALL-CAPS headlines.
- In market-wrap titles, a stock keeps full confidence only as the clause subject: the
  sole stock named before the index term, or directly followed by a price-move verb
  (optionally "shares"/"stock" in between) and not the tail of a list ("X, Y fall").
- Changing an article's text (extraction, --reclean) clears `articles.linked_at`, so the
  next entities run re-links it.
- `published_at` is genuinely unreliable: CarDekho pages from 2010-2011 arrive via Google
  News with 2026 dates. Date-dependent linking rules (e.g. TMPV's post-demerger "Tata
  Motors" rule) inherit that error.
- torch comes from PyTorch's CPU wheel index (`[tool.uv.sources]` in pyproject.toml), so
  no platform resolves the CUDA build. Don't add torch-dependent packages that pin a
  CUDA torch.
- Sentiment (`processing/sentiment.py`): FinBERT at the commit pinned in
  `config/news_sources.yaml`. It's loaded from the local Hugging Face cache (downloaded
  once, ~440 MB) and runs offline. `DISABLE_SAFETENSORS_CONVERSION` is set because
  otherwise transformers calls the Hub from a background thread on every load. Rows are
  keyed by model_name. Re-linking an article deletes its sentiment rows so they're
  rescored. Tests mock the scorer; the one real-model test skips if the model isn't
  cached.
- `news_daily` assigns news to the IST session it can first affect (after 15:30 or on a
  non-trading day -> next trading day). Trading days come from stored price dates, so
  run the price collector before sentiment for correct holiday handling. The table is
  rebuilt from scratch each run. News time = min(published_at, first_seen_at); for
  backtests, only use a row after its `latest_first_seen_at`.
- FinBERT scores the tone of the text, not the tone for our stock: "X wins order from
  Reliance" scores positive for RELIANCE.
- `run_update.py` runs prices, then indicators, then the news steps (collect -> text ->
  stories -> entities -> sentiment -> news_daily), then the filings steps (HDFC Bank IR
  PDFs -> results inbox import -> filing classification -> results rebuild). Every step is
  isolated through `run_steps`: a failure is logged and recorded, later steps still run,
  and the exit code is 1. Raise `StepError` for "finished, but partly failed". News and
  filings modules are imported lazily inside `news_steps()`/`filings_steps()`, so a broken
  dependency (e.g. torch, pdfplumber) can't stop price updates. Keep it that way: don't
  import them at the top of run_update.py.
- Results filings keep `filing_type = "results"` through classification (a test checks
  this), because `results.rebuild` selects files by that type.
- A results file's `filed_at` is the board-approval date stated in the file (XBRL field, or
  "results … approved by the Board … held on <date>" in PDFs). When the file doesn't state
  one, it's the quarter end + 45 days and the subject says "[date approx.]"; the
  dashboard labels those markers "(date approx.)". `results.rebuild` refreshes these
  fields for already-stored files.
- Dashboard news: the news list collapses copies into one row per story (earliest copy
  shown, "+N more sources" = other outlets in the story_id). The sentiment card and panel
  read `news_daily`, so they only change after an update rebuilds it. Headlines go
  through `escape_markdown` because Streamlit treats `$` as LaTeX.
- Article text is stored for personal analysis only (see README "Data use"). Never add
  features that publish or share stored article text.
- Filings: there is no filings collector yet. NSE's Terms of Use prohibit automated
  collection; BSE's API needs a browser session and its terms forbid reproduction without
  written consent. The source is undecided (see the Phase 3 investigation). Don't build
  NSE/BSE scraping without an explicit decision. The `filings` table and
  `processing/filing_categories.py` are source-agnostic.
- Filing categories: a board meeting "to consider" X is a board_meeting, never an action.
  Corporate actions found in filings go to `pending_actions` with a status; the code must
  never write `config/corporate_actions.yaml` (there's a test for this). Because Yahoo
  usually adjusts splits and bonuses, "yahoo_adjusted" (no ex-date gap in raw prices) means
  don't add it; "needs_review" means the gap is real and it probably belongs in the YAML.
- Results come from files, never from scraping exchanges: XBRL the user downloads into
  `data/filings/inbox/` (identified by content, any filename), plus HDFC Bank results PDFs
  from its IR site (the only IR site whose results are statically linked and robots-allowed;
  Infosys/TCS block bots, TMPV lists results via a private JS API). XBRL always beats PDF
  for the same (symbol, quarter, basis); PDF rows are trust=low. The `results` table is
  rebuilt from stored files on every run.
- XBRL: elements are matched by local name (prefixes differ between taxonomies). Values are
  absolute INR, divided by 1e7 for crore. Q4 filings also contain full-year contexts, so
  the parser only accepts ~3-month contexts. Bank XBRL tag names in `XBRL_TAGS` are
  UNVERIFIED until a real HDFC Bank XBRL is imported.
- HDFC Bank's PDFs are scanned images with an OCR text layer: expect "eamed", "18187 49"
  (lost decimal), "3170830,09" (decimal comma). `repair_ocr_numbers` handles these. Some
  PDFs (Q4 FY26, Q1 FY25) have no text layer at all and are skipped; some links 404
  intermittently.
- Per-share figures are as reported: HDFC Bank's pre-Aug-2025 EPS is on the pre-bonus
  share count and isn't restated.
- Moneycontrol RSS is frozen (newest items 2024) and Business Standard RSS returns 403 to
  non-browser clients; both are excluded from `config/news_sources.yaml`. Don't spoof a
  browser User-Agent to get around blocks.
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
