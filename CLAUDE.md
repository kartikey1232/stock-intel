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
uv run python run_update.py [--skip-news] [--skip-filings] [--skip-social] [--failures-file PATH]  # prices -> indicators -> news -> filings -> social
uv run python -m collectors.prices              # prices only
uv run python -m processing.indicators [--full] # indicators only (watchlist + benchmarks)
uv run python -m processing.signals [--report]  # rebuild rule-based price signals; counts
uv run python -m processing.backtest [--csv PATH]  # event study of stored signals vs Nifty 50
uv run python -m collectors.prices --universe nifty50_ex_watchlist  # research universe prices
uv run python -m processing.hypotheses          # registered out-of-sample test (docs/hypotheses.md)
uv run python -m processing.alerts [--date D] [--days N] [--digest]  # build alerts; print digest
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
uv run python -m collectors.valuepickr          # ValuePickr posts (needs confirmed: true + SOCIAL_HASH_KEY)
uv run python -m collectors.valuepickr --reclean  # re-apply text cleaning rules to stored raw_html (local)
uv run python -m processing.social [--full] [--report]  # link posts, score, rebuild social_daily
uv run streamlit run dashboard.py               # launch the dashboard
```

## Gotchas

- `yf.download` never raises: it catches every per-ticker error, rate limits (HTTP 429)
  included, and returns an empty frame. The price collector therefore uses
  `Ticker.history` with `yf.config.debug.hide_exceptions = False`, which raises
  `YFRateLimitError`, `YFPricesMissingError`, HTTP errors, etc. (checked on yfinance 1.7).
  A rate limit gets one retry after 90 s; if it persists, the remaining stocks are skipped
  and fail. An empty response is always a failure, because every request starts at a date
  that has a bar.
- After prices, every stock must have a bar for the latest completed session (from 16:00
  IST on a trading day, else the previous trading day; `FINAL_BAR_TIME`, because Yahoo's
  final bar can arrive after the 15:30 close). Signals use the same cut-off. News and
  social session assignment still uses the 15:30 close. Trading days = weekdays minus
  `config/market_holidays.yaml`, maintained by hand from NSE's holiday circular (never
  fetched: principle 7). Add next year's list each December; an unlisted holiday only
  causes a false failure that day.
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
- Scheduling: `scripts/install_schedule.sh` generates a per-user LaunchAgent
  (`~/Library/LaunchAgents/com.stockintel.update.plist`, weekdays 16:15 local time = IST)
  that runs `scripts/scheduled_update.sh`. launchd gives jobs a minimal environment, so the
  wrapper uses absolute paths (uv in ~/.local/bin), cds to the project, waits up to 5 min
  for the network after wake, and logs to `logs/update-YYYY-MM-DD.log`. Re-run the
  installer after moving the project, because the plist stores absolute paths. On a
  non-zero exit it shows a macOS notification (osascript) listing the failed steps, which
  run_update.py writes to `--failures-file`; success is silent. Simulate a failure with
  `UV=<fake uv script> LOG_DIR=<tmp dir> scripts/scheduled_update.sh`.
- File paths stored in the database (`filings.attachment_path`) are project-relative;
  resolve them with `storage.db.project_path()` and store them with
  `to_project_relative()`. Never store absolute paths: the project lives in
  ~/Developer/stock-intel because launchd jobs can't read ~/Desktop (macOS privacy
  protection), and it may move again.
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
- Filings: there is no exchange filings collector, and there won't be one without written
  consent (principle 7). NSE's Terms of Use prohibit automated collection; BSE's terms
  forbid reproduction without written consent. The `filings` table and
  `processing/filing_categories.py` are source-agnostic, so a collector can be added later
  without changing them.
- HDFC Bank IR fetching (`collectors/results_ir.py`) is allowed only under the personal,
  non-commercial exception in HDFC Bank's Website Usage Terms (hdfc.bank.in, checked
  2026-09-23): no bot clause, robots.txt allows `*`, but site content may be entered into a
  database only when downloaded "for my own personal, non-commercial use". If the project
  becomes a product (multiple users, commercial use), switch HDFC Bank to the results
  inbox or get written permission first. Don't add another company's IR site without
  checking its terms the same way.
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
  the parser only accepts ~3-month contexts. Pre-2025 NSE files (FY25Q2, FY25Q3) give the
  year-to-date context (`FourD`) the quarter's period dates; each context's
  `DateOfStartOfReportingPeriod`/`DateOfEndOfReportingPeriod` facts override its period
  dates, so the parser never relies on document order. Bank tags in `XBRL_TAGS` are
  verified against HDFC Bank's FY27Q1 files: net NPA is `NonPerformingAssets` (no "Net"),
  NPA ratios are fractions (0.0117, stored as 1.17 %), and consolidated
  `ProfitLossForThePeriod` is before minority interest (owners' profit is
  `ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates`). Consolidated
  bank files put 0 in the NPA fields; those placeholders are dropped.
- HDFC Bank's PDFs are scanned images with an OCR text layer: expect "eamed", "18187 49"
  (lost decimal), "3170830,09" (decimal comma). `repair_ocr_numbers` handles these. Some
  PDFs (Q4 FY26, Q1 FY25) have no text layer at all and are skipped; some links 404
  intermittently.
- Results are as originally filed; XBRL carries no restated comparatives. When a quarter
  reports discontinued operations (TMPV FY26Q2: the CV demerger, ₹82,616 cr gain), its top
  line excludes that business but earlier quarters include it, so `results.changes` adds
  `qoq_note`/`yoy_note` for comparisons that span it (shown in the dashboard and
  `--report`). The demerger's accounting quarter (FY26Q2, to 30 Sep) is earlier than its
  price ex-date (14 Oct), so detect it from the filings, not `corporate_actions.yaml`.
- Results validation flags can be acknowledged in `config/acknowledged_flags.yaml`, keyed
  by (symbol, quarter, basis, metric) and pinned to the exact flag text. A match sets
  `results.flag_reviewed` to the reason, logs at INFO, shows "~" in `--report` and
  "reviewed: <reason>" in the dashboard. New flags, or changed text for an acknowledged
  key, still WARN. Acknowledge only after checking the figure against the filing; never
  add entries to silence a flag you haven't explained.
- Per-share figures are as reported: HDFC Bank's pre-Aug-2025 EPS and RELIANCE's FY25Q2
  EPS (before its Oct-2024 1:1 bonus) are on the pre-bonus share count and aren't restated.
- Moneycontrol RSS is frozen (newest items 2024) and Business Standard RSS returns 403 to
  non-browser clients; both are excluded from `config/news_sources.yaml`. Don't spoof a
  browser User-Agent to get around blocks.
- Social (Phase 4): only ValuePickr so far (`collectors/valuepickr.py`, Discourse JSON).
  It makes no requests until `confirmed: true` in `config/social_sources.yaml` and needs
  `SOCIAL_HASH_KEY` in .env. Every run re-reads robots.txt and checks each URL against it
  (never use /search or RSS: robots.txt disallows them); 1 request per 5 s; a 429 waits
  Retry-After once (if <= `max_retry_after_s`), then the run stops. Incremental runs
  fetch posts after the topic's `last_post_id`; the first run takes the newest
  `backfill_posts`. Discovered topics come from /latest.json by title (stock news terms).
- Social deletion sync: posts missing from a topic's stream, or deleted/withdrawn/hidden
  on re-check (`recheck_posts` oldest-checked per run), are hard-deleted with their
  mentions and sentiment; a topic returning 403/404/410 loses all its posts. An *edit* is
  only what Discourse reports (higher post `version`, else later `updated_at`), never a
  difference in our cleaned text; text that changes because our cleaning rules changed is
  "re-cleaned" and logged separately. Both kinds get the new text and are re-linked.
- Social raw HTML: `social_posts.raw_html` is the post's HTML after privacy stripping
  (`sanitize_cooked`: quotes, @mentions, anything with a username, images, and every
  attribute except `class` are removed). `text` = `extract_text(raw_html)`. After
  changing `extract_text`/`TEXT_XPATH`, run `collectors.valuepickr --reclean` (local).
  Never widen what `sanitize_cooked` keeps without checking it stores no identities. So stored history is what's still visible today,
  not what was visible then: Phase 5 backtests must treat social data as
  survivorship-biased, and filter on `first_seen_at`.
- Social text cleaning drops quotes of other posts, @mentions, code, images and oneboxes
  (other people's words and names). Posts in a stock's dedicated topic link with
  confidence 0.9 (`method = thread`); general and discovered topics use the entity
  linker on the post text. TMPV's topic 1233 defaults to TMPV only for posts before
  2025-10-14; after that the linker decides, but in a stock's own topic a conditional
  alias that meets its context ("Tata Motors" + a PV term) scores at least 0.6
  (`OWN_THREAD_CONDITIONAL`); elsewhere one such mention scores 0.45 and doesn't link.
- Social collector logs: every run logs how many /latest.json topics were checked and
  matched (even 0), and per topic how many requested posts weren't stored and why (not
  a regular post, deleted/hidden, no text after cleaning, not returned).
- Social scoring (`processing/social.py`): link text (stored as `⟦…⟧` by the collector,
  for posts fetched or re-checked since this rule; older text only gets URL and headline
  removal), URLs and pasted headlines ("<headline> - The Economic Times", sources in
  `config/social_sources.yaml` `scoring.headline_sources`) are removed before scoring;
  they're kept for linking. A post then under `scoring.min_words` (6) words is a `share`
  (it had links/headlines) or `short`; both count in post_count but aren't scored
  (`social_mentions.post_kind`). Line breaks split sentences only after .!?…:; so
  hard-wrapped lines are rejoined.
- **Social history is uneven by stock and is recent context only.** Each topic's first
  fetch took only its newest 200 posts (`backfill_posts`), so coverage starts in 2018
  for INFY but 2024 for HDFCBANK, and posts deleted upstream are removed. Never use
  social data as backtest history or to compare stocks with each other; use it only as
  recent context for one stock.
- `social_daily` counts linked posts and distinct `author_hmac`s per IST session (same
  session rules as news_daily); mean/weighted scores cover scored posts only and are
  NULL when none were scored. The dashboard's Social tab shows counts, scores and links,
  never post text.
- pandas-ta 0.4 emits RSI from bar 2; `processing/indicators.py` masks the warm-up.
  pandas-ta is a beta release pinned in `uv.lock`, and it caps numpy at 2.2 via numba.
- Signals (`processing/signals.py`, parameters only in `config/signals.yaml`): rebuilt
  from full adjusted history every run into `signals`. No look-ahead: a signal on day t
  uses only bars up to t's close (backward rolling windows; the volume average excludes
  t; bars after the latest completed session are dropped). Rules must stay ratios or
  comparisons and `value` scale-free (%, multiple, RSI), because back-adjusting for a
  later corporate action rescales all earlier bars; then a later action can't change a
  past signal. `tests/test_signals.py` replays history bar by bar and fails if any day's
  signals differ from the full-history ones; keep it passing for every new rule.
- `min_gap_pct` (ma_cross, default 0) is display/alert-only: `display_signals` swaps raw
  crosses for `confirmed_crosses`, dated when the SMAs first stand min_gap_pct apart
  without crossing back (causal). The stored `signals` table and the backtest always use
  raw crosses. A filter on the stored cross-day value would be wrong: every cross's
  spread is tiny on the day it crosses (0.001-0.3% here).
- Event study (`processing/backtest.py`, settings in `config/backtest.yaml`): entry at
  the next open after a signal (a signal is only known at the close), h-day return =
  open(t+1) -> close(t+h), excess = minus Nifty 50 over the identical window. edge =
  direction x (event excess - the same stock's all-days excess), so bearish signals
  "work" when the stock underperforms. Same-signal/same-stock events within
  `cluster_gap` (10) bars chain into one. n < 30 = "too few events to judge". The
  report must keep stating its limits (survivorship, multiple testing, no costs,
  dependent events). Don't tune signal parameters on these results: it overfits.
- Hypotheses: register them in `docs/hypotheses.md` (dated, parameters frozen, decision
  rule and data rules written down) and commit that before fetching or testing
  out-of-sample data. Never edit a registered entry or tune parameters to it: append
  results, and put any new idea in a new dated entry tested on new data. Post-hoc checks
  must be labelled as such and never change a verdict.
- Research universes (`config/universes.yaml`, e.g. `nifty50_ex_watchlist`, 45 Nifty 50
  stocks from a Wikipedia list dated 2025-12-08) are price-only. They're collected with
  `collectors.prices --universe`, never by `run_update.py`, and get no news, social,
  filings, indicators or dashboard entry. Only registered signals are computed on them.
  They have no corporate actions recorded: Yahoo doesn't always adjust (TRENT
  2026-01-01, -33%, looks unadjusted).
- Alerts (`processing/alerts.py`, `config/alerts.yaml`) are attention flags, never advice.
  No alert or digest text may match `FORBIDDEN_RE` (buy/sell/accumulate/target price/
  recommend...): `build_alerts` refuses to store it, and third-party headlines that read
  like tips (`TIP_HEADLINE_RE`) are never quoted. Every price-signal alert carries its
  event-study note (in-sample verdict at `backtest_note.horizon`, plus the out-of-sample
  status kept by hand in `config/alerts.yaml`; update it after each registered test).
  Alerts are keyed on (symbol, alert_type, subject), so re-runs never duplicate; `sent_at`
  stays NULL until delivery exists. Results alerts only fire for the latest quarter and
  board dates within `max_age_days` (backfilled history never alerts); pending-action
  alerts only for the latest day; a news shift only on its first day. run_update records
  each run in `pipeline_runs` before building the day's alerts.
- Benchmarks (`benchmarks:` in `config/watchlist.yaml`, e.g. NIFTY50 = ^NSEI) get prices,
  the missing-bar check and indicators like stocks, but `load_watchlist()` never returns
  them: no news, social, filings, signals or dashboard entry.
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
7. **No automated access to NSE or BSE, by any means, until written consent is received.**
   This covers every host of both exchanges (websites, APIs, archives such as
   nsearchives.nseindia.com, RSS feeds) and every method: scripts, the Chrome extension, a
   headless browser, or `fetch()` from inside a page session. It applies even to one-off
   investigation or debugging. Reading their terms of use or public documentation pages
   is fine. Exchange data comes only from files the user downloads by hand into
   `data/filings/inbox/`.
8. **Social data is personal and non-commercial.** ValuePickr content is licensed CC
   BY-NC-SA 3.0: store and analyse it for personal, non-commercial use only; if this
   becomes a product, get ValuePickr's permission first. Never store usernames, display
   names, avatars or profile links (authors only as a keyed HMAC), never display or share
   post text, and delete posts that are deleted or hidden upstream.
9. **Reddit: nothing until Reddit approves an API application; build nothing for it
   now.** Reddit data comes only from the official Data API under an approved app: no
   scraping (any method, including the Chrome extension or a headless browser), no
   Pushshift, no third-party Reddit datasets or APIs. When a Reddit collector is built,
   it must:
   - delete post text as soon as the post is scored, and never keep it longer than 48 h;
   - keep only post ID, timestamp, stock link and score (enough to trace deletions);
   - store no author information at all: no username, no user ID, no hash of either;
   - run a deletion sync that removes the scores of posts deleted or removed on Reddit
     and recomputes the affected aggregates;
   - never use Reddit data, or anything derived from it, in fitted or trained models, or
     for research (research needs Reddit's Research Data Access (RFR) programme).
   Reddit-derived scores may feed dashboards and rule-based signals only.

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
