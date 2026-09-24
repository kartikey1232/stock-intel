# stock-intel

A personal stock intelligence tool for Indian markets (NSE/BSE). It collects daily prices
from Yahoo Finance, computes technical indicators, and shows them in a Streamlit dashboard.

**Current status:** Phases 1 (prices + indicators) and 2 (news + sentiment) are complete.
Phase 3 (filings and results) works from a manual results inbox plus HDFC Bank's IR site;
automated exchange access is on hold pending the exchanges' consent. Phase 4 (social
signals) has a ValuePickr collector; Reddit waits on Reddit's API approval. Backtesting
and Telegram alerts are planned. See [Roadmap](#roadmap).

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

One command updates everything for every stock in the watchlist, in five stages:

1. prices (the watchlist plus the Nifty 50 benchmark);
2. indicators, then rule-based price signals;
3. the news pipeline (collect articles, extract text, group stories, link articles to
   stocks, score sentiment, build daily aggregates);
4. the filings pipeline (HDFC Bank results PDFs from its IR site, import the results
   inbox, classify filings, extract results);
5. the social pipeline (ValuePickr posts, link them to stocks, score sentiment, build
   daily aggregates).

```bash
uv run python run_update.py                  # everything
uv run python run_update.py --skip-news      # without the news pipeline
uv run python run_update.py --skip-filings   # without the filings pipeline
uv run python run_update.py --skip-social    # without the social pipeline
```

A rejected inbox file makes the run exit with `1`; the reason is in
`data/filings/inbox/rejected/`.

The first run with news downloads the FinBERT model (~440 MB, a few minutes). A news
failure never blocks price updates, and each news step runs even if an earlier one
failed. The exit code is `1` if anything failed.

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
uv run python -m processing.signals --report    # rebuild price signals; counts per stock
uv run python run_update.py --full-indicators   # both, with a full indicator recompute
```

Logs go to the console and to `logs/stock_intel.log` (rotating, UTC timestamps).

## Collecting news

```bash
uv run python -m collectors.news
```

This fetches Google News (one India-edition query per stock) plus publisher RSS feeds
from Economic Times and Mint, and stores raw articles in the `articles` table. Articles
are keyed by a hash of the cleaned-up URL (tracking parameters, fragments and trailing
slashes removed), so re-running only adds new ones. Each article records `published_at`
(from the feed, which can be wrong) and `first_seen_at` (when we first fetched it).
Requests are rate-limited per domain and retried on network errors, 429 and 5xx.

Sources live in [`config/news_sources.yaml`](config/news_sources.yaml). A stock's Google
query uses its `news_terms` from the watchlist, or its name and first alias.

Then fetch article text and group duplicates:

```bash
uv run python -m collectors.article_text   # full text for articles still 'pending'
uv run python -m processing.stories        # assign story_id to new articles
uv run python -m processing.entities       # link articles to the stocks they're about
uv run python -m processing.sentiment --report   # FinBERT sentiment + daily per-stock scores
```

- **Text extraction** fetches each page (respecting robots.txt and per-domain rate
  limits) and extracts the main text with trafilatura, dropping site boilerplate lines
  listed in the config. Each article ends up `ok`, `paywalled` (text left empty; title and
  summary kept), `skipped` (Google News link or disallowed by robots.txt) or `failed`.
  Transient failures are retried on later runs, at most 3 attempts in total.
- **Story grouping** gives copies of the same story (syndicated or rewritten by other
  outlets within 48 hours) a shared `story_id`, using title similarity. Nothing is
  deleted. Count stories, not articles, so one wire report doesn't look like five.
  The threshold is `stories.similarity_threshold` in the config.
- **Entity linking** decides which watchlist stocks each article is about, using the
  name rules in the watchlist. It skips other companies that share a name (HDFC Life,
  Reliance Power), requires finance context for ambiguous names like "HDFC", and applies
  date-aware rules (after the demerger, bare "Tata Motors" counts for TMPV only when the
  sentence is about passenger vehicles). Each stock's all-caps NSE symbol (RELIANCE,
  INFY) always counts. Each match gets a confidence: a title mention scores highest, a
  single mention in a long article lowest, and stocks listed in market wraps score low,
  unless the stock drives its own clause ("…; HDFC Bank jumps 2.5%"). A labelled set of 67 headlines checks it
  (`uv run python -m processing.entities --evaluate`).
- **Sentiment** scores each article–stock link with FinBERT (ProsusAI/finbert, pinned
  commit, CPU). The input is the headline plus only the sentences that mention the stock.
  `score` = P(positive) − P(negative), from −1 to 1. The model (~440 MB) downloads on
  first run and is loaded from the local cache afterwards.
- **Daily aggregates** (`news_daily`) summarise each stock per IST trading session:
  story and article counts, mean and confidence-weighted score, and strongly negative
  stories. Copies of a story count once. News after 15:30 IST or on a non-trading day
  counts towards the next session.

## Daily schedule (macOS)

A launchd job runs `run_update.py` every weekday at 16:15 IST, after the market closes at
15:30 and after 16:00, when a day's bar counts as final:

```bash
scripts/install_schedule.sh            # install / reinstall
scripts/install_schedule.sh --remove   # disable and remove
```

- Each run logs to `logs/update-YYYY-MM-DD.log`; logs older than 60 days are deleted.
- If anything fails, a macOS notification names the failed steps and the log file.
  Successful runs are silent. Failures include Yahoo rate limits, empty Yahoo responses,
  and any stock missing a bar for the latest completed trading day. Trading days come
  from `config/market_holidays.yaml`: add NSE's holiday list for each new year.
- It runs as you, without a terminal open, while you're logged in (a locked screen is
  fine). It doesn't run while you're logged out.
- If the Mac is asleep at 16:15, the run happens as soon as it wakes; several missed days
  collapse into one run. If the Mac is shut down, the run is skipped.
- Check it: `launchctl print gui/$(id -u)/com.stockintel.update | grep -E "runs|last exit"`,
  and look at today's log.

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
- **Signals on chart** (sidebar): a checkbox per rule-based signal. Bullish markers sit
  below the candle, bearish ones above; hover for the value. Defaults come from
  `default_on` in `config/signals.yaml`.
- **RSI** (with 30/70 levels) and **MACD** (with histogram) panels.
- **News sentiment panel:** daily story-weighted sentiment (line, −1 to +1) and story
  count (bars) per trading session, on the same date axis as the price chart.
- **News sentiment card:** 7-day average compared with the 30-day average.
- **Results markers:** dotted lines on the price chart at each results date. Dates the file
  didn't state are labelled "(date approx.)".
- **Pending corporate actions:** a warning banner for announced splits, bonuses or demergers
  that may need adding to `config/corporate_actions.yaml`.
- **Tabs:**
  - **News:** linked headline, source, IST time, sentiment badge, and "+N more sources"
    when other outlets carried the same story.
  - **Filings:** date, category badge, subject and an attachment link or download, with a
    category filter.
  - **Results:** quarterly revenue (total income for banks) and net profit bars with YoY %
    lines, plus a last-8-quarters table with YoY/QoQ, source (XBRL or lower-trust PDF) and
    validation flags; standalone or consolidated.

Data is cached for 5 minutes. Use **Reload from database** in the sidebar after running an
update.

## Exchange filings (Phase 3, in progress)

`processing/filing_categories.py` sorts filings into our own types (results, board_meeting,
dividend, corporate_action, shareholding_pattern, press_release, credit_rating,
insider_trading, analyst_meet, other) while keeping the exchange's label. It extracts
announced splits, bonuses, demergers, rights issues and buybacks (ratio, record date,
ex-date) and compares them with `config/corporate_actions.yaml` in a `pending_actions`
table. It never edits the YAML: each action gets a status (recorded, upcoming, undated,
yahoo_adjusted, needs_review, no_adjustment) telling you whether to add it.

```bash
uv run python -m processing.filing_categories
```

There is no filings collector yet. NSE's terms prohibit automated collection, and how
filings will be sourced (BSE, a manual inbox, or licensed data) is still undecided.

HDFC Bank's [Website Usage Terms](https://www.hdfc.bank.in/useful-links/website-usage-terms)
(checked 2026-09-23) don't mention bots, and its robots.txt allows all agents, but they
forbid entering site content into a database except what you download "for my own
personal, non-commercial use". The automatic IR PDF download (`collectors.results_ir`)
relies on that exception. If this project becomes a product, switch HDFC Bank to the
results inbox or get HDFC Bank's permission first.

## Quarterly results

Results are stored in long format (`results` table): one row per symbol, quarter, basis
(standalone/consolidated) and line item, in ₹ crore (EPS in ₹/share, NPA ratios in %).
Headline metrics get QoQ/YoY changes and validation flags (a >5x jump or an unexpected
sign change usually means a unit or parsing error). Once you've checked a flag and it's a
real event (e.g. TMPV's demerger quarters), record it in
[`config/acknowledged_flags.yaml`](config/acknowledged_flags.yaml) with the exact flag text
and a reason: it then logs at INFO and shows as "reviewed" in the dashboard (`~` in
`--report`). If the flag's text changes later, it warns again.

Where the files come from:

- **XBRL you download** (preferred, trust = high). Run the checklist to see what's missing
  and the exact NSE page for each file, save the XBRL (.xml, not iXBRL) into
  `data/filings/inbox/` with any name, then import:

  ```bash
  uv run python -m collectors.result_files checklist
  uv run python -m collectors.result_files import
  ```

  Files are identified by their content (company, quarter, standalone/consolidated).
  Anything that doesn't parse goes to `inbox/rejected/` with a `.reason.txt`.
- **HDFC Bank results PDFs** from its investor-relations site, fetched automatically
  (trust = low, used only where no XBRL exists):

  ```bash
  uv run python -m collectors.results_ir
  ```

```bash
uv run python -m processing.results --report   # last 8 quarters of revenue and net profit
```

## Social signals (Phase 4)

**ValuePickr** ([forum.valuepickr.com](https://forum.valuepickr.com)) is a Discourse forum.
Its terms have no bot or API clause, its robots.txt allows the JSON pages we use, and
user posts are licensed CC BY-NC-SA 3.0, so collection is for **personal, non-commercial
use only**. `config/social_sources.yaml` lists the topics to follow:

- **Dedicated topics** (HDFC Bank, RIL, Infosys, Tata Motors): every post is about that
  stock. The Tata Motors thread counts as TMPV only for posts before the demerger
  (14 Oct 2025); later posts go through the entity linker.
- **General discussion** comes only from recent topics in `/latest.json` whose title
  names a watchlist stock; their posts are linked by the entity linker. ("Market news
  and updates" was left out: no posts since February 2024.)

The topic IDs were confirmed in a browser on 2026-09-24 (`confirmed: true`). Changing
them later: check each one on the forum first. The collector also needs a hash key in
`.env`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # put this in SOCIAL_HASH_KEY
uv run python -m collectors.valuepickr                      # one collection pass
uv run python -m processing.social --report                 # link, score, aggregate
```

What is stored: post id, topic, post number, link, creation time, text (quotes of other
posts, @mentions, code and images removed), like count, and a keyed hash of the author's
numeric id (to count distinct authors). **No usernames, names, avatars or profile links.**
Posts deleted or hidden on ValuePickr are deleted here, with their derived rows, and
posts ValuePickr reports as edited (its own version number) are re-scored. A
privacy-stripped copy of each post's HTML is kept, so changed cleaning rules are
re-applied locally with `uv run python -m collectors.valuepickr --reclean` instead of
being mistaken for edits. The collector sends an honest User-Agent, makes one request
every 5 seconds, and stops if the forum keeps answering HTTP 429.

Only a post's own words are scored: link text, URLs and pasted news headlines are
removed first (a headline would duplicate the news signal). Posts left with fewer than 6
words are counted as activity (a *share* or a *short reply*) but not scored.

Social history is uneven: each topic's first fetch took only its newest 200 posts, so
coverage starts years apart by stock, and deleted posts are removed. Treat it as recent
context for one stock, not as backtest history or a cross-stock comparison.

The dashboard's **Social** tab shows post and author counts, sentiment and links to the
posts, never the post text.

**Reddit**: nothing is built, and nothing will be until Reddit approves an API
application. Reddit data will come only from the official API, never from scraping,
Pushshift or third-party datasets. A future collector will delete post text as soon as
it's scored (within 48 hours), keep only post ID, timestamp, stock link and score, store
no author information at all, remove the scores of deleted posts and recompute
aggregates, and never feed fitted models or research (see CLAUDE.md principle 9).

## Data use

**This repository contains code only.** No collected data is published here: no prices
database, news article text, forum posts, exchange filings or results files. Those
stay on the machine that collects them (`data/` and `.env` are git-ignored), in line with
the sources' terms.

Article text and forum posts are fetched and stored **locally, for personal analysis
only**. They belong to their publishers and authors: don't republish them, share the
database, or expose the text through a public service. Collection respects robots.txt and rate limits, and paywalled articles
are never extracted.

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
  aliases: [Infosys, Infy]   # names that always count as a mention
  news_terms: [Infosys]      # optional: exact phrases for the Google News query
  ambiguous_aliases: []      # optional: names needing finance context (e.g. HDFC)
  exclude_patterns: [Infosys Foundation]   # optional: phrases that never count
  conditional_aliases: []    # optional: names needing specific context from a date on
```

The file is validated on load. Missing, unknown or duplicate fields raise a clear error.
New stocks get their full 5-year history on the next update.

**Environment (`.env`):**

| Variable | Used for | Default |
|---|---|---|
| `DB_PATH` | SQLite database file (relative to project root) | `data/stock_intel.db` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts and digest (Phase 6) | — (not sent if unset) |
| `SOCIAL_HASH_KEY` | Keyed hash of social post authors (Phase 4) | — (required for ValuePickr) |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Reddit (Phase 4, awaiting approval) | — |
| `ANTHROPIC_API_KEY` | News sentiment (Phase 2) | — |

## Project structure

```
.
├── run_update.py          # Entry point: collect prices → compute indicators
├── dashboard.py           # Streamlit + Plotly dashboard
├── config/
│   ├── watchlist.yaml     # Stocks to track, plus benchmark indices (Nifty 50)
│   ├── signals.yaml       # Rule-based price signal definitions and parameters
│   ├── signals.py         # Loads and validates signal definitions
│   ├── backtest.yaml      # Event-study horizons, clustering, bootstrap settings
│   ├── backtest.py        # Loads and validates event-study settings
│   ├── universes.yaml     # Price-only research universes (45 other Nifty 50 stocks)
│   ├── universes.py       # Loads research universes
│   ├── alerts.yaml        # Alert types, severities, thresholds, backtest-note settings
│   ├── alerts.py          # Loads and validates alert settings
│   ├── loader.py          # Loads and validates the watchlist
│   ├── corporate_actions.yaml  # Splits/bonuses/demergers (reviewed in git)
│   ├── corporate_actions.py    # Loads and validates corporate actions
│   ├── acknowledged_flags.yaml # Results validation flags reviewed by hand
│   ├── acknowledged_flags.py   # Loads and validates acknowledged flags
│   ├── news_sources.yaml  # RSS feeds, Google News settings, rate limits
│   ├── news_sources.py    # Loads and validates news sources
│   ├── social_sources.yaml  # ValuePickr topics and politeness settings
│   └── social_sources.py    # Loads and validates social sources
├── collectors/            # Fetch and store RAW data only (no analysis)
│   ├── prices.py          # Daily OHLCV from Yahoo Finance
│   ├── news.py            # Raw articles from Google News + publisher RSS
│   ├── result_files.py    # Results inbox importer + missing-files checklist
│   ├── results_ir.py      # HDFC Bank results PDFs from its IR site
│   ├── valuepickr.py      # Raw ValuePickr posts (Discourse JSON) + deletion sync
│   └── article_text.py    # Article full text (trafilatura), robots.txt-aware
├── processing/            # Turn raw data into insight (no network calls)
│   ├── adjustments.py     # Adjusted OHLC + unrecorded-gap detection
│   ├── stories.py         # Groups syndicated article copies into stories
│   ├── entities.py        # Links articles to the watchlist stocks they're about
│   ├── sentiment.py       # FinBERT scoring + daily per-stock aggregates
│   ├── social.py          # Links social posts to stocks, scores them, social_daily
│   ├── filing_categories.py  # Filing types + announced corporate actions
│   ├── results.py         # XBRL/PDF results extraction, QoQ/YoY, validation
│   ├── signals.py         # Rule-based price signals (no look-ahead)
│   ├── backtest.py        # Event study of signals vs Nifty 50 and a do-nothing baseline
│   ├── hypotheses.py      # Registered out-of-sample test (docs/hypotheses.md)
│   ├── alerts.py          # Daily alerts (attention flags) and the template digest
│   └── indicators.py      # RSI, MACD, SMA, EMA, Bollinger, ATR via pandas-ta
├── delivery/            # Send alerts and digests out (no collecting or analysis)
│   └── telegram.py        # Telegram Bot API over httpx; token never logged
├── storage/
│   ├── db.py              # SQLAlchemy schema, upserts, reads
│   └── social.py          # Reads and writes for the social tables
├── utils/
│   ├── http.py            # Per-domain rate limiter + retried GET
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
- `alerts` (`symbol, alert_type, subject`): alert_date, severity, text, created_at,
  sent_at (NULL until delivery exists). `pipeline_runs` (`started_at`): finished_at,
  exit_code, failures.
- `signals` (`symbol, date, signal`): direction (bullish/bearish/neutral), value
  (scale-free: %, a volume multiple or an RSI level), computed_at.
- `corporate_actions` (`symbol, ex_date`): action_type, price_factor, source, note.
  A mirror of `config/corporate_actions.yaml`.
- `articles` (`id` = hash of the cleaned-up URL): url, source, title, summary,
  published_at, first_seen_at, fetched_via, text, text_status, text_attempts,
  text_error, story_id, linked_at. The feed fields are written once and never
  overwritten; text, story and linking fields are filled in by later steps.
- `article_mentions` (`article_id, symbol`): matched_alias, location (title / summary /
  body), mention_count, confidence (0-1). A confidence of 0.5 or more means the article
  is about that stock.
- `article_sentiment` (`article_id, symbol, model_name`): model_version, label,
  p_positive, p_negative, p_neutral, score, computed_at.
- `news_daily` (`symbol, session_date, model_name`): story_count, article_count,
  mean_score, weighted_score, strong_negative_stories, latest_first_seen_at.
- `social_topics` (`platform, topic_id`): title, slug, role (dedicated/general/discovered),
  fetched_at, last_post_id, last_post_number.
- `social_posts` (`id` = "platform:post id"): topic_id, post_number, url, text,
  author_hmac, likes, created_at, first_seen_at, checked_at, linked_at. Deleted when the
  post is deleted or hidden upstream.
- `social_mentions` (`post_id, symbol`): method (thread/linker), matched_alias,
  confidence. `social_sentiment` (`post_id, symbol, model_name`): as `article_sentiment`.
- `social_daily` (`platform, symbol, session_date, model_name`): post_count,
  author_count, scored_count, mean_score, weighted_score, latest_first_seen_at.
- `filings` (`id`): exchange, exchange_id, symbol, filed_at, first_seen_at, category (the
  exchange's label), subject, description, attachment_url/path/sha256, duplicate_of,
  filing_type, filing_tags. Empty until a collector or importer exists.
- `pending_actions` (`id`): symbol, action_type, ratio, price_factor, record_date,
  ex_date, status, note, filed_at, subject. Rebuilt on every run.
- `results` (`symbol, period_end, basis, metric`): fiscal_quarter, value, unit, source
  (xbrl/pdf), trust, filing_id, extracted_at, flag. Rebuilt from stored files.

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
- **News:**
  - Google News links are stored as Google redirect URLs, because they can't be resolved
    reliably to the publisher's URL. The same story can therefore appear twice (via
    Google and via a publisher feed).
  - Google caps each query at 100 results; with daily runs and a 7-day window this is
    rarely hit.
  - Moneycontrol (frozen feeds) and Business Standard (blocks non-browser clients) are
    not used directly; their stories arrive through Google News.
  - Full text is only available for publisher-feed articles (Economic Times, Mint).
    Google News articles are `skipped` because their links can't be resolved. Paywall
    detection relies on the schema.org `isAccessibleForFree` flag.
  - Entity linking is rule-based. Known misses: a bare "Reliance" without finance words
    ("Ambani says Reliance will invest…"). Share-price pages that still call TMPV "Tata
    Motors" are deliberately not linked, because after the demerger "Tata Motors" means
    the CV company.
  - FinBERT scores the tone of the text, not its effect on the stock. "Supplier wins an
    order from Reliance" reads as positive, and the model was trained on English
    financial news, not Indian market phrasing.
  - Results: bank XBRL tags are unverified until a real HDFC Bank XBRL is imported.
    Scanned PDFs without a text layer can't be read (no OCR). EPS isn't restated for
    bonuses or splits.
  - Story grouping uses titles only. It errs on the side of keeping stories apart (a
    heavily reworded copy becomes its own story). A known false merge: the alias "HDFC"
    also matches "HDFC Mutual Fund", so separate mutual-fund lists can group together.
- **Yahoo Finance** is an unofficial data source. Holiday filler bars (zero volume, flat
  price) are filtered out, but occasional gaps or revisions are possible.

## Price signals (Phase 5)

`config/signals.yaml` defines each signal and all its parameters: golden/death cross
(SMA 50/200), RSI crossing below 30 / above 70, volume above 2x its 20-day average, 52-week
high/low breakouts, and gaps over 3%. `processing/signals.py` evaluates them on
corporate-action adjusted daily bars and stores them in `signals` (symbol, date, signal,
direction, value), rebuilt from full history on each update.

**Event study.** `uv run python -m processing.backtest` measures what followed each
signal at 1, 5, 20 and 60 trading days: the return from the next day's open, the excess
over Nifty 50 for the same window, and the "edge" over the same stock's excess return
from all days (doing nothing). Consecutive firings count once. It reports n, mean and
median excess, hit rate, and a bootstrap 95% CI per signal, marks n < 30 as too few
events, and states its limits: 5 hand-picked large caps (survivorship bias), 36 results
tested at once, no transaction costs.

**Out-of-sample checks.** Hypotheses are pre-registered in
[`docs/hypotheses.md`](docs/hypotheses.md) with frozen parameters, then tested on a
price-only universe of 45 other Nifty 50 stocks (`config/universes.yaml`, collected with
`uv run python -m collectors.prices --universe nifty50_ex_watchlist`, never in the daily
update) with `uv run python -m processing.hypotheses`. The first two (gap continuation,
H1 and H2) were not supported out of sample.

A signal on a given day uses only data available at that day's close. A test replays
the price history one bar at a time and fails if any day's signals would change once
later bars arrive, including a later split. Signals are for the watchlist only; the
Nifty 50 (`^NSEI`, under `benchmarks:` in `config/watchlist.yaml`) is stored as a
benchmark price series with indicators, not shown as a stock.

## Alerts and daily digest (Phase 6)

After each update, `processing/alerts.py` builds the day's alerts into the `alerts`
table (no delivery yet). Types and thresholds are in `config/alerts.yaml`: new results
for the latest quarter (with YoY/QoQ), a price move over 3% or an opening gap (with that
session's news and filings), 52-week breakouts and SMA crosses, a shift in 7-day news
sentiment vs 30-day, pending corporate actions, and pipeline failures. Each price-signal
alert says how that signal did in the event study (e.g. "historically no edge vs doing
nothing at 20 trading days (n=40)"): alerts are attention flags, and no alert text may
recommend buying or selling. Re-running never duplicates an alert.

```bash
uv run python -m processing.alerts --digest            # today's alerts and digest
uv run python -m processing.alerts --days 10 --digest  # rebuild the last 10 trading days
```

The digest is template-based: one line per stock ("quiet day" if nothing happened), the
alerts, and the pipeline status of that day's scheduled run.

**Telegram.** After each update, high-severity alerts (new results, pending corporate
actions, pipeline failures) are sent immediately, one message each, then the digest once.
Sent alerts get `sent_at`, so nothing is sent twice. If Telegram isn't configured or
fails, the macOS notification still reports failed runs. Setup:

1. Create a bot with @BotFather and put its token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send the bot any message, then run `uv run python -m delivery.telegram --find-chat`
   and put the chat id it prints in `.env` as `TELEGRAM_CHAT_ID`.
3. `uv run python -m delivery.telegram --test` sends a test message.

The token is never written to logs or error messages.

## Roadmap

1. ✅ Prices + technical indicators
2. ✅ News + sentiment
3. NSE/BSE filings (quarterly results)
4. Social media signals (ValuePickr done; Reddit awaiting API approval)
5. Signals & backtesting (price signals and an event study done)
6. Scheduling, daily digests, Telegram alerts (scheduling, alert engine and digest done;
   delivery next)
