# Pre-registered hypotheses

Registered **2026-09-24**, before any out-of-sample data was fetched or tested. This file
is committed on its own, ahead of the collector and the test, so the order is visible
in git history. Don't edit a registered entry; add a new dated entry instead.

## 2026-09-24: gap continuation after the watchlist event study

The in-sample event study (5 watchlist stocks, `processing/backtest.py`, commit
`7b59d31`) found 3 of 36 results with a 95% CI excluding zero, about 1.8 expected by
chance. Two are registered here to test out of sample; nothing else is.

- **H1:** after gap_down (>3%), excess return vs Nifty is negative at 5 and 20 trading
  days (continuation).
- **H2:** after gap_up (>3%), excess return vs Nifty is negative at 1 day.

### Frozen parameters

No parameter changes are allowed for testing these. The values are those in the files
at commit `071e977`:

| File | Git blob | Relevant settings |
|---|---|---|
| `config/signals.yaml` | `f71efcaa49d5d695429a03355f4112037d32ad63` | gap_up: open > previous close by more than 3.0%, direction bullish; gap_down: open below previous close by more than 3.0%, direction bearish; no cooldown |
| `config/backtest.yaml` | `f74b2710f667704ab1856c57b954b734f13c26b4` | horizons 1/5/20/60 (only 1, 5 and 20 are used here); entry at the next open; benchmark NIFTY50; cluster_gap 10; min_events 30; 10,000 bootstrap samples; seed 20260924 |

### Test design (fixed before the data)

- **Universe:** the Nifty 50 constituents listed on Wikipedia's "NIFTY 50" page (list
  dated 8 December 2025), excluding the 5 watchlist stocks (RELIANCE, TCS, HDFCBANK,
  INFY, TMPV): 45 stocks, listed in `config/universes.yaml`. The official list is on
  NSE/NSE Indices sites, which this project doesn't access automatically (principle 7).
  Prices only: no news, social or filings.
- **Data:** Yahoo daily bars from the existing price collector (same rate limits and
  missing-bar check), the same period as the watchlist study: bars from 2021-09-24 to
  2026-09-24. Stocks listed later contribute from their first bar.
- **Measurement:** exactly as in `processing/backtest.py`. A trade starts at the next
  day's open. Excess is measured against Nifty 50 over the same window. edge = signal
  direction × (event excess − the same stock's all-days excess). Same-signal,
  same-stock events within 10 bars count once.
- **Decision rule.** A hypothesis *holds* only if n ≥ 30 and the 95% bootstrap CI of the
  edge lies entirely on the predicted side of zero:
  - H1: edge CI > 0 at both 5 and 20 days (gap_down is bearish, so a positive edge means
    the stock did worse than its baseline);
  - H2: edge CI < 0 at 1 day (gap_up is bullish, so a negative edge means the stock did
    worse than its baseline).

  Mean excess return and its CI are reported as a secondary measure; they don't decide
  the outcome, because a stock that lags Nifty generally has negative excess after any
  event.
- **Corporate actions:** there are no recorded corporate actions for the universe, and
  Yahoo adjusts splits and bonuses but not demergers. So events are excluded when they
  fall on a known demerger ex-date listed in `config/universes.yaml` (ITC, ITC Hotels
  demerger, 2025-01-06), or when the overnight move is beyond ±25% (an unrecorded
  corporate action, by the same rule as `processing/adjustments.py`). Excluded events
  are listed in the report.
- **Scope:** only gap_up and gap_down are computed on this universe. No other signal is
  tested on it.

### Known limits

- Current constituents only: stocks that left the index or were delisted during the
  period are missing (survivorship bias). The list may also miss rebalances after
  8 December 2025.
- Events cluster on market-wide gap days across stocks, and the 20-day windows overlap,
  so events aren't independent and the bootstrap CI is too narrow.
- No transaction costs, taxes or slippage.
