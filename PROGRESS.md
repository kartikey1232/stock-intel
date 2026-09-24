# stock-intel progress log

Where the project stands, what was decided and why, and what to do next. Read this with
`CLAUDE.md` (rules, commands, gotchas) and `README.md` (usage) when resuming work.

Last updated: 2026-09-24 (ValuePickr collector commit, after `4a49eb5`).

## Status at a glance

| Phase | Scope | Status |
|---|---|---|
| 1 | Prices + technical indicators | Done |
| 2 | News + sentiment | Done |
| 3 | NSE/BSE filings (quarterly results) | Done: all 80 XBRL files (8 quarters × standalone/consolidated × 5 stocks) imported by hand; flags reviewed |
| 4 | Social media signals | ValuePickr collector built and tested, topic IDs confirmed, not yet run (needs `SOCIAL_HASH_KEY`); Reddit awaits API approval |
| 5 | Signals & backtesting | Not started |
| 6 | Scheduling, digests, Telegram alerts | Scheduling + macOS failure notifications done; digests and Telegram not started |

- Tests: 369 passing (`uv run pytest`), ruff clean.
- Watchlist (`config/watchlist.yaml`): RELIANCE, TCS, HDFCBANK, INFY, TMPV.
- Git: `main`, no remote pushes made from these sessions. `PROGRESS.md` is tracked.

## Data on disk (as of 2026-09-24)

| Data | Coverage |
|---|---|
| Prices | 1,234 daily bars per stock, ~5 years, latest 2026-09-23; indicators up to date |
| News | 665 articles (Google News per stock + ET and Mint RSS); 422 article-stock mentions |
| Results files | 83 stored: 80 XBRL (FY25Q2–FY27Q1, standalone + consolidated, all 5 stocks) and 3 HDFC Bank IR PDFs (FY22Q1, FY23Q1, FY27Q1) |
| Results table | 4,128 rows, rebuilt from the stored files on every run; 14 validation flags, all TMPV and all acknowledged |

The database is `data/stock_intel.db` (from `DB_PATH` in `.env`); `data/` and `logs/` are
not in git.

## Timeline (commits made 2026-09-23 and 2026-09-24)

| Commit | What |
|---|---|
| `5120a74` | Project skeleton |
| `1c11e40` | Phase 1: Yahoo price collector, indicators, Streamlit dashboard |
| `28a0800` | Corporate-action adjustments (`config/corporate_actions.yaml`); fixed the TMPV demerger price gap |
| `e5ed04a` | Phase 2: news collection, article text, story grouping, entity linking, FinBERT sentiment |
| `7655c91` | Entity linking: NSE symbols as strong aliases, Land Rover/Range Rover/Defender for TMPV, market-wrap subject rule. Labelled set 67 headlines, precision 1.00, recall 0.97 |
| `0ddcb6c` | Phase 3: filing categories, quarterly results from XBRL/PDF, dashboard wiring |
| `5a1a71c` | Principle 7: no automated NSE/BSE access until written consent |
| `5c0e622` | Weekday 16:15 IST launchd schedule; DB file paths stored project-relative |
| `50098ea` | Failure visibility: explicit Yahoo rate-limit handling, empty response = failure, missing-bar check, macOS notification on failure |
| `f3f741e` | Bank XBRL tags verified on HDFC Bank FY27Q1; provisions metric added |
| `c3fbb86` | QoQ/YoY notes across discontinued operations (TMPV demerger); fixed `--report` flag marker |
| `5865308` | XBRL contexts use each context's declared reporting period (pre-2025 files mislabel the year-to-date context); HDFC Bank IR terms documented |
| `4a49eb5` | `config/acknowledged_flags.yaml`: reviewed flags log at INFO and show as reviewed; PROGRESS.md tracked |
| (next) | Phase 4: ValuePickr collector, social linking/sentiment/`social_daily`, dashboard Social tab, `--skip-social`, principles 8–9 |

## Decisions and findings worth remembering

### Prices and scheduling
- yfinance 1.7.0: `yf.download` swallows every per-ticker error, rate limits included, and
  returns an empty frame. The collector uses `Ticker.history` with exceptions un-hidden.
  A 429 gets one retry after 90 s, then the remaining stocks are skipped and fail.
- Every price request starts at a date that already has a bar, so an empty response is a
  failure, never "no new data".
- Missing-bar check: after 15:30 IST on a trading day, every stock must have that day's
  bar or the run fails that day. Trading days = weekdays minus
  `config/market_holidays.yaml`.
- **The 2026 holidays after 14 Sep in `market_holidays.yaml` are unverified** (2 Oct,
  20 Oct, 10 Nov, 24 Nov, 25 Dec). Check them against NSE's circular, and add 2027 in
  December. A missing holiday only causes a false failure that day.
- The launchd job (`com.stockintel.update`) is installed. On failure,
  `scripts/scheduled_update.sh` shows a macOS notification listing the failed steps
  (from `run_update.py --failures-file`). Success is silent. The notification path was
  tested with a fake `uv`; whether it showed on screen was not confirmed.
- The only scheduled run so far (2026-09-23 22:16 IST, before `50098ea`) exited 1: the
  HDFC Bank IR PDF step hit a read timeout and then a DNS failure. The same run's log shows
  Yahoo briefly returning empty data for RELIANCE and TMPV, which the old code treated as
  "no new data".

### Compliance
- **No automated access to NSE or BSE by any method** (CLAUDE.md principle 7). Exchange
  data comes only from files downloaded by hand into `data/filings/inbox/`.
- HDFC Bank's Website Usage Terms (checked 2026-09-23 at
  https://www.hdfc.bank.in/useful-links/website-usage-terms) don't mention bots or
  scraping, and `hdfc.bank.in/robots.txt` allows `*`. They do forbid "enter into a
  database … except that which I may download for my own personal, non-commercial use".
  So the IR PDF step stays enabled for personal use, but **must be revisited if this
  becomes a product**. The old `hdfcbank.com` URLs 301-redirect to `hdfc.bank.in`; the
  collector already uses the new domain.
- Infosys and TCS IR sites block bots; TMPV lists results through a private JS API. Those
  companies use the inbox only.

### Results (Phase 3)
- Bank XBRL tags were verified against HDFC Bank's real files: net NPA is
  `NonPerformingAssets`, NPA ratios are fractions (stored ×100 as %), and consolidated
  profit uses `ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates`
  (`ProfitLossForThePeriod` is before minority interest). Consolidated bank files put 0
  in the NPA fields; those placeholders are dropped.
- PDF rows ending in "(Refer note 8)" used to read 8 as the first column; note
  references are now stripped before reading numbers.
- Hand checks against published results matched exactly: RELIANCE FY26Q1, TCS FY26Q3,
  HDFCBANK FY26Q1 and FY27Q1 (XBRL vs PDF), INFY FY26Q3 (SEC 6-K), TMPV FY26Q2.
  RELIANCE's ₹2,73,252 cr "revenue" in the press is gross revenue; we store revenue
  from operations (₹2,48,660 cr).
- Reconciliation check: for RELIANCE, TCS, HDFCBANK and INFY, the four FY26 quarters sum
  exactly to the full-year figures in the Q4 XBRL.
- **TMPV and the demerger:** the CV business left the accounts in FY26Q2 (quarter to
  30 Sep 2025), before the price ex-date of 14 Oct 2025. FY25Q4 and FY26Q1 as filed
  include CV, so the FY26 quarters sum ₹16,729 cr above the full-year revenue. FY26Q1 on
  a like-for-like basis is about ₹87,678 cr (derived, not reported), not ₹1,04,407 cr.
  `results.changes` adds `qoq_note`/`yoy_note` whenever a comparison spans a quarter with
  discontinued operations; the dashboard and `--report` show them. TMPV's profit flags
  (demerger gain ₹82,616 cr in FY26Q2, loss in FY26Q3) are real events, and all 14
  (FY26Q2–FY27Q1, both bases, net profit and EPS) are acknowledged in
  `config/acknowledged_flags.yaml`.
- **Pre-2025 XBRL (FY25Q2, FY25Q3):** NSE's older files give the year-to-date context
  (`FourD`) the quarter's period dates. The parser had picked the quarter only by
  document order; it now reads each context's `DateOfStartOfReportingPeriod`. Values
  didn't change.
- Hand checks of the older files (raw XML vs stored values, all exact): RELIANCE, TCS,
  HDFCBANK FY25Q2; INFY, TMPV FY25Q3.
- RELIANCE FY25Q2 EPS (24.48) is before its Oct-2024 1:1 bonus; FY25Q3 onward is after.
  Not restated, like HDFC Bank's pre-Aug-2025 EPS.
- YoY exists only from FY26Q2: no FY24 quarters are stored. Download FY24Q2–FY25Q1 files
  if longer YoY history is wanted.

### Social (Phase 4)
- Investigation (2026-09-24), from terms and docs only, no platform requests:
  - Reddit: Data API Terms (rev. 20 Jul 2026) and Developer Terms (rev. 24 Mar 2026)
    read verbatim. New API access needs manual approval under the Responsible Builder
    Policy (Nov 2025); that page and the Data API Wiki return 403 to non-browser clients
    and were only seen via search excerpts, so read them in a browser before applying.
    PRAW 8.0.3 (Aug 2026) is current. Only the user can apply.
  - ValuePickr: ToS (2018) silent on bots; posts CC BY-NC-SA 3.0; robots.txt disallows
    /search and RSS only; Discourse default limit 50 req/10 s per IP.
  - X: pay-per-use only (~$0.005/read), skip. StockTwits: API closed. YouTube: 10k
    units/day, 30-day storage rule, later maybe. Telegram: bans AI/ML use of data, defer.
- Decisions (approved by the user): dedicated topics link at 0.9 without the linker;
  TMPV topic 1233 defaults only before 2025-10-14; ValuePickr text kept (the 30-day text
  deletion is for Reddit only); deletion sync applies to both; authors stored only as a
  keyed HMAC; dashboard shows no post text.
- Topic IDs came from search results and were confirmed by the user in a browser on
  2026-09-24: HDFCBANK 24141, RELIANCE 32873, INFY 8124, TMPV 1233 (all active).
  "Market news and updates" (133414) was dropped: no posts since February 2024. General
  discussion comes only from /latest.json topics. TCS has no dedicated thread; it relies
  on the linker in discovered topics.
- A single bare "Tata Motors" in a PV sentence after the demerger scores 0.45 and doesn't
  link (existing linker rule); posts naming JLR/TMPV do.

## Open items / next steps

1. Each new quarter: download its standalone and consolidated XBRL for all 5 stocks into
   `data/filings/inbox/`, run `collectors.result_files import`, then `checklist`. Review
   any new WARNING flag against the filing before acknowledging it.
2. Verify the unverified 2026 holidays (see above) and add 2027.
3. Confirm the macOS failure notification actually appears (System Settings →
   Notifications → Script Editor must be allowed).
4. Watch the next few scheduled runs (`logs/update-YYYY-MM-DD.log`). Check how often the
   HDFC Bank IR step hits network errors; consider whether a failure there should only
   warn.
5. Optional: show provisions (and NII/NPA for banks) on the dashboard; they're already
   extracted.
6. Phase 4: add `SOCIAL_HASH_KEY` to `.env` (topic IDs are confirmed), then run
   `collectors.valuepickr` once by hand and check the log. Apply for Reddit API access if
   wanted (see README). Phase 5 backtests must filter news and social posts on
   `first_seen_at` and results on `filed_at`, and treat social data as
   survivorship-biased (deleted posts are purged).

## Resuming

```bash
cd ~/Developer/stock-intel
git log --oneline -5 && git status        # expect a clean tree
uv sync && uv run pytest -q               # expect 332+ passing
uv run python -m collectors.result_files checklist   # results coverage
uv run python -m processing.results --report         # last 8 quarters per stock
tail -50 logs/update-$(date +%F).log      # latest scheduled run
uv run streamlit run dashboard.py
```
