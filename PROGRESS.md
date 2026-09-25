# stock-intel progress log

Where the project stands, what was decided and why, and what to do next. Read this with
`CLAUDE.md` (rules, commands, gotchas) and `README.md` (usage) when resuming work.

Last updated: 2026-09-24 (after the first ValuePickr run; own-thread boost commit, after `9d826a4`).

## Status at a glance

| Phase | Scope | Status |
|---|---|---|
| 1 | Prices + technical indicators | Done |
| 2 | News + sentiment | Done |
| 3 | NSE/BSE filings (quarterly results) | Done: all 80 XBRL files (8 quarters × standalone/consolidated × 5 stocks) imported by hand; flags reviewed |
| 4 | Social media signals | Done: ValuePickr collecting (4 dedicated topics, first run 2026-09-24). Reddit out of scope (application denied 2026-09-24) |
| 5 | Signals & backtesting | 9 rule-based price signals (no look-ahead test), Nifty 50 benchmark, event study built; no signal tuned |
| 6 | Scheduling, digests, Telegram alerts | Scheduling, alert engine, template digest and Telegram delivery done (live 2026-09-24); macOS notification is the fallback |

- Tests: 494 passing (`uv run pytest`), ruff clean.
- Watchlist (`config/watchlist.yaml`): RELIANCE, TCS, HDFCBANK, INFY, TMPV.
- Git: `main`, pushed to the public repo https://github.com/kartikey1232/stock-intel
  (created 2026-09-24 after a history scan for secrets and personal data).

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
| `4f522d2` | Phase 4: ValuePickr collector, social linking/sentiment/`social_daily`, dashboard Social tab, `--skip-social`, principles 8–9 |
| `9d826a4` | ValuePickr topics confirmed; "Market news and updates" (133414) dropped |
| `518de02` | Own-thread conditional boost (0.6); discovery and skipped-post logging |
| (next) | Social scoring: link text/URLs/pasted headlines excluded, shares and short replies unscored, broken lines rejoined |

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
    PRAW 8.0.3 (Aug 2026) is current. (The application was later denied; see below.)
  - ValuePickr: ToS (2018) silent on bots; posts CC BY-NC-SA 3.0; robots.txt disallows
    /search and RSS only; Discourse default limit 50 req/10 s per IP.
  - X: pay-per-use only (~$0.005/read), skip. StockTwits: API closed. YouTube: 10k
    units/day, 30-day storage rule, later maybe. Telegram: bans AI/ML use of data, defer.
- Decisions (approved by the user): dedicated topics link at 0.9 without the linker;
  TMPV topic 1233 defaults only before 2025-10-14; ValuePickr text kept; ValuePickr
  authors stored only as a keyed HMAC; dashboard shows no post text.
- **Reddit is permanently out of scope (CLAUDE.md principle 9).** Reddit denied the Data
  API application on 2026-09-24 (request 18508777) as not compliant with the Responsible
  Builder Policy and/or lacking details. No Reddit collector will be built, the same use
  case won't be re-submitted (the policy prohibits multiple requests), and no scraping,
  Pushshift or third-party Reddit datasets. This replaces the earlier plan to build a
  Reddit collector after approval.
- Topic IDs came from search results and were confirmed by the user in a browser on
  2026-09-24: HDFCBANK 24141, RELIANCE 32873, INFY 8124, TMPV 1233 (all active).
  "Market news and updates" (133414) was dropped: no posts since February 2024. General
  discussion comes only from /latest.json topics. TCS has no dedicated thread; it relies
  on the linker in discovered topics.
- A single bare "Tata Motors" in a PV sentence after the demerger scored 0.45 and didn't
  link; in TMPV's own topic such a match now scores 0.6 (`OWN_THREAD_CONDITIONAL`).
- First ValuePickr run (2026-09-24, by the user): 195/167/177/173 posts for
  24141/32873/8124/1233, the newest 200 post ids per topic (`backfill_posts`); 5-33 per
  topic weren't stored (reason unknown for this run: logging added afterwards). Stored
  ranges: HDFCBANK #732-952 (Feb 2024-Sep 2026), RELIANCE #189-407 (Jan 2021-Aug 2026),
  INFY #121-332 (Sep 2018-Sep 2025), TMPV #384-597 (Nov 2022-Feb 2026). The /latest.json
  step ran but matched nothing and logged nothing (now logged).
- Processing: 712 posts, 706 linked (all scored), 326 social_daily rows from 2018-09-14
  to 2026-09-04. Only 8 posts in 1233 are post-demerger: 2 link via JLR/TMPV names, 6
  don't (CV talk or no company named); the new boost didn't change any of them.
- FinBERT on forum text: about 70% of posts score neutral. Tuning (2026-09-24): link
  text, URLs and pasted headlines are no longer scored; posts under 6 words of their own
  are shares (6) or short replies (16) and count as activity only; broken lines are
  rejoined. After rescoring, 684 of 706 linked mentions are scored; mean scores barely
  moved (HDFCBANK 0.004, INFY 0.002, RELIANCE 0.114, TMPV 0.017).
- Link-text markers apply to posts fetched or re-checked after this change; the 712
  existing posts get them over ~8 runs of the rolling re-check (100 posts per run).
- The scheduled run on 2026-09-24 logged "100 re-checked, 0 deleted, 7 edited". All 7
  (32873 #195, #196, #205; 8124 #165, #199, #201, #212; posted 2019-2021) were our
  link-marker rule, not edits: edit detection compared cleaned text. It now uses
  Discourse's `version` (fallback `updated_at`), and rule changes count as "re-cleaned".
  Posts now also store privacy-stripped `raw_html`, so rules can be re-applied locally
  (`--reclean`). Existing posts gain raw_html/version on their next re-check.
- Social data is recent context only (uneven backfill per stock, deletions removed):
  never backtest history or cross-stock comparison (CLAUDE.md).

### Signals (Phase 5)
- 9 signals in `config/signals.yaml`. Direction choices: RSI below 30 = bullish
  (oversold, contrarian), above 70 = bearish; volume spikes take the day's close-to-close
  direction; 52-week breakouts have a 20-bar cooldown so a run of new highs is one signal.
- First full run (2026-09-24, bars to 2026-09-24): 687 signals, 2021-10-11 to 2026-09-18.
  Per signal (HDFCBANK/INFY/RELIANCE/TCS/TMPV): golden 3/4/3/2/3, death 3/4/4/2/3, RSI<30
  18/30/19/22/19, RSI>70 20/9/20/18/29, volume spike 53/67/54/65/64, 52w high 5/4/5/6/9,
  52w low 3/4/4/8/4, gap up 7/9/6/5/21, gap down 10/16/2/6/15.
- TMPV's golden cross (2025-10-29) and death cross (2025-11-19) after the demerger are a
  genuine whipsaw with SMA spreads of +0.02%/-0.03%; the demerger itself produces no gap
  signal (adjusted prices). A minimum-spread parameter would suppress such whipsaws.
- The look-ahead test was checked by injecting a one-bar leak into the volume average:
  the bar-by-bar replay test fails; the simpler "append future bars" test alone did not.

### Event study (2026-09-24, untuned signals, data to 2026-09-24)
- Setup: entry at next open, 1/5/20/60-day horizons, excess vs Nifty 50, edge vs the same
  stock's all-days excess, cluster gap 10 bars, 10,000 bootstrap samples (seed 20260924).
- Only volume spikes (n~175), RSI<30 (~63), RSI>70 (~59), gap up (~40) and gap down
  (~40) have n >= 30. Golden/death crosses (15/16) and 52-week breakouts (29/23) are too
  few to judge.
- Of 36 results, 3 have a CI excluding 0 (about 1.8 expected by chance): gap_down edge
  +1.47% at 5 days and +1.63% at 20 days (the stock kept underperforming after a gap
  down; the two horizons overlap, so not independent evidence), and gap_up -0.60% at 1
  day (gap-ups underperformed the next day). Everything else: no clear difference.
- The five stocks' baseline excess vs Nifty is mostly negative over the period, so raw
  mean excess after signals is negative for most signals; read the edge column.
- Not acted on: no signal parameters were changed. Treat the gap results as hypotheses
  to recheck on new data, not findings.

### Out-of-sample test (2026-09-24)
- H1 (gap_down continuation at 5/20 days) and H2 (gap_up underperformance at 1 day) were
  registered in `docs/hypotheses.md` (commit `d031f56`) before any data was fetched, with
  parameters frozen at `071e977`.
- Universe: 45 other Nifty 50 stocks (Wikipedia list dated 2025-12-08, unverified against
  NSE's official list), prices 2021-09-24 to 2026-09-24 via `collectors.prices
  --universe`. SUNPHARMA timed out on the first pass and succeeded on the retry.
- Result: H1 not supported (edge +0.19%/+0.27%, CIs include 0, n~272). H2 not supported
  (edge -0.28% [-0.60%, +0.04%], n=302). A post-hoc check without TRENT (an unadjusted
  -33% move on 2026-01-01) puts H2 at -0.34% [-0.65%, -0.01%]; labelled post hoc, so it
  doesn't change the verdict.
- The in-sample gap results were most likely chance findings, as the multiple-testing
  note warned.

### Small fixes (2026-09-24)
- The missing-bar check and signals treat a session as complete from 16:00 IST
  (`FINAL_BAR_TIME`), not 15:30, because Yahoo's final bar can arrive late. News/social
  session assignment still uses the 15:30 close.
- `min_gap_pct` (ma_cross, default 0) confirms crosses for display and alerts only. With
  real data, 0.25% would hide 1 of 31 crosses (an HDFCBANK death cross), 0.5% would hide 2
  (plus TMPV's 2025-10-29 golden cross), 1% would hide 3. TMPV's 2025-11-19 death cross is
  confirmed on 2025-11-21 at 0.5%. Left at 0: choose a value before relying on alerts.
- `config/signals.yaml` changed (min_gap_pct lines added), so its git blob no longer
  matches the one recorded in `docs/hypotheses.md`; the gap_up/gap_down settings the
  registration froze are unchanged.

### Alerts (2026-09-24)
- Built for the last 10 trading days (2026-09-10 to 2026-09-24): 7 alerts. 15 Sep: gap-ups
  for INFY (+4.6% open), TCS (+3.4%), TMPV (+6.3%), and RELIANCE's 52-week low breakdown;
  17 Sep: TMPV +4.5%; 18 Sep: TCS -3.9%, TMPV -3.4%. 24 Sep: quiet for all five.
- No news for the 15 Sep alerts: news collection started 2026-09-23 with a 7-day Google
  window, so linked news before ~16 Sep is sparse.
- No results alerts: all results were backfilled (board dates up to 2026-08-12, older than
  max_age_days). No pending corporate actions exist. Pipeline status reads "no run
  recorded" until the next scheduled run, which is the first to write pipeline_runs.

### Telegram (2026-09-24)
- `delivery/telegram.py` is live since 2026-09-24 20:16 IST: bot @Kartikey_stockintel_bot,
  chat 1101031608 (the user's account, display name "Aaryan"), test message delivered.
  The first token was revoked and replaced before going live, because the user's "/start"
  and "Hi" (19:56) never showed up in getUpdates, and something outside this project may
  have read them with that token. A later "test" never reached the bot either; it was
  probably sent in another chat. Nothing on this Mac polls Telegram, and our code never
  passes offset or allowed_updates. No token-shaped strings appear in logs/.
- From the next scheduled run, high alerts and the digest go to Telegram; the macOS
  notification only appears if Telegram fails.
- Found while testing: the first redaction regex started with `\b`, which never matches
  a token inside "/bot<token>/" URLs; fixed and covered by tests.

### Alert fixes before going live (2026-09-24)
- The advice filter wrongly refused "buy-back", "sell-off" and "sold" (hyphens are word
  boundaries); now allowed. "buy" on its own, even in "buy in", is still refused.
- The 7 alerts of 10-24 Sep were rebuilt (none had been sent). The 15 Sep gap-ups (INFY,
  TCS, TMPV) are marked market-wide: after the 14 Sep holiday Nifty opened +0.8% and
  closed -1.2%, and the three stocks gave back much of their gaps. Gap-up notes now quote
  the 1-day in-sample result (worse than baseline, n=43), matching H2's horizon.

### Backups (2026-09-25)
- BACKUP_DIR = iCloud Drive `Backups/stock-intel` (user's choice). A one-off launchd job
  (same bash -> uv -> python chain as the schedule) wrote, read and deleted a file there
  without Full Disk Access; the interactive agent shell is blocked from that folder.
- First real backup via launchd: stock-intel-20260925-130821.tar.gz, 8.4 MB (4.4 MB of it
  the database, the rest 83 filings files); 14 days ~ 118 MB. Restored via launchd into a
  temporary folder: SHA-256 and integrity check OK; every table's row count and all
  filings identical to the live data.
- Copies are in iCloud, i.e. off the Mac, but on one Apple account.

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
6. Phase 4: check the next ValuePickr run's log for the skipped-post reasons and the
   /latest.json line. Phase 5 backtests must filter news and social posts on
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
