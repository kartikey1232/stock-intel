"""Daily update: prices, indicators, signals, news, filings and social posts.

Steps:
  1. prices       collect daily OHLCV for the watchlist and benchmarks (collectors.prices),
                  then check each has a bar for the latest completed trading session
  2. indicators   sync corporate actions, compute indicators for the watchlist and
     + signals    benchmarks (processing.indicators), then rebuild rule-based price
                  signals for the watchlist (processing.signals, config/signals.yaml)
  3. news         collect articles -> article text -> story grouping -> entity linking
                  -> sentiment -> daily aggregates (skip with --skip-news)
  4. filings      HDFC Bank results PDFs from its IR site -> import the results inbox
                  (data/filings/inbox/) -> INFY/HDFCBANK results from SEC 6-Ks
                  (validated against XBRL) -> classify filings -> extract results
                  (skip with --skip-filings). There is no exchange filings collector:
                  NSE/BSE access is undecided (see README).
  5. social       ValuePickr posts -> stock links -> sentiment -> daily aggregates
                  (skip with --skip-social)
Then the run and its failed steps are recorded in pipeline_runs, the day's alerts are
built (processing.alerts), the database and data/filings/ are backed up to BACKUP_DIR
(storage.backup; skip with --skip-backup), and high-severity alerts plus the digest are
sent to Telegram (delivery.telegram). A failure in any of these is reported too. When
Telegram delivered, the failures file ends with TELEGRAM_DELIVERED so the scheduled
wrapper skips its macOS notification; otherwise that notification is the fallback.

Corporate actions are synced from config/corporate_actions.yaml before indicators are
computed; symbols whose actions changed get a full indicator recompute.

Every step runs even if an earlier one failed: indicators run for symbols whose price
download failed (they keep their previous data), and news runs after price problems.
Each news, filings and social step is isolated the same way, so a failing feed, file or model
never blocks the rest. Exit code is 0 on full success, 1 if anything failed.
--failures-file writes the failed steps one per line (empty on success), for the
scheduled wrapper's notification.

Run with:  uv run python run_update.py [--full-indicators] [--skip-news] [--skip-filings]
                                       [--skip-social] [--skip-backup]
                                       [--failures-file PATH]
"""

import argparse
import datetime as dt
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from collectors.prices import collect_all, log_summary, missing_session_bars
from config.loader import PriceSeries, Stock, load_benchmarks, load_watchlist
from processing.adjustments import sync_actions_from_config
from processing.indicators import process_all
from processing.signals import run as compute_signals
from storage.db import init_db, record_pipeline_run
from utils import setup_logging

logger = logging.getLogger("run_update")


class StepError(RuntimeError):
    """A pipeline step finished but reported failures (e.g. some feeds or files failed)."""


def news_steps(stocks: list[Stock]) -> list[tuple[str, Callable[[], None]]]:
    """The news pipeline as (name, step) pairs, run in order. Imports are lazy so a
    broken news dependency can't stop price updates."""
    from collectors import article_text, news
    from config.news_sources import load_news_sources
    from processing import entities, sentiment, stories

    config = load_news_sources()

    def collect() -> None:
        results = news.collect_all(config, stocks)
        news.log_summary(results)
        failed = [r.name for r in results if r.error]
        if failed:
            raise StepError(f"{len(failed)} source(s) failed: {', '.join(failed)}")

    def score() -> None:
        scorer = sentiment.FinbertScorer(
            config.sentiment_model, config.sentiment_revision, config.sentiment_batch_size
        )
        sentiment.score_pending(config, stocks, scorer)

    return [
        ("news collection", collect),
        ("article text", lambda: article_text.log_summary(article_text.extract_pending(config))),
        ("story grouping", lambda: stories.run(config, stocks)),
        ("entity linking", lambda: entities.run(stocks)),
        ("sentiment", score),
        ("daily news aggregates", lambda: sentiment.rebuild_daily(config)),
    ]


def filings_steps(stocks: list[Stock]) -> list[tuple[str, Callable[[], None]]]:
    """The filings pipeline as (name, step) pairs, run in order (lazy imports, as for news)."""
    from collectors import result_files, results_ir
    from processing import filing_categories, results

    def ir_pdfs() -> None:
        failures = results_ir.collect_all(stocks)
        if failures:
            raise StepError(f"IR collection failed for {', '.join(failures)}")

    def sec_6ks() -> None:
        from collectors import sec_results  # imported here: a broken import fails only this step

        failures = sec_results.collect_all(stocks)
        if failures:
            raise StepError(f"SEC 6-K collection failed: {failures}")

    def inbox() -> None:
        outcomes = result_files.import_inbox(stocks)
        for o in outcomes:
            (logger.warning if o.status == "rejected" else logger.info)(
                "inbox %s %s: %s", o.status, o.name, o.detail
            )
        rejected = [o.name for o in outcomes if o.status == "rejected"]
        if rejected:
            raise StepError(
                f"{len(rejected)} inbox file(s) rejected (see data/filings/inbox/rejected/): "
                + ", ".join(rejected)
            )

    return [
        ("IR results PDFs", ir_pdfs),
        ("results inbox import", inbox),
        ("SEC 6-K results", sec_6ks),
        ("filing classification", lambda: filing_categories.run()),
        ("results extraction", lambda: results.rebuild(stocks)),
    ]


def social_steps(stocks: list[Stock]) -> list[tuple[str, Callable[[], None]]]:
    """The social pipeline as (name, step) pairs, run in order (lazy imports, as for news)."""
    from collectors import valuepickr
    from config.news_sources import load_news_sources
    from config.social_sources import load_social_sources
    from processing import sentiment, social

    news_config = load_news_sources()
    social_config = load_social_sources()

    def collect() -> None:
        result = valuepickr.collect_all(social_config, stocks)
        valuepickr.log_summary(result)
        if result.failures:
            raise StepError("; ".join(result.failures))

    def score() -> None:
        scorer = sentiment.FinbertScorer(news_config.sentiment_model,
                                         news_config.sentiment_revision,
                                         news_config.sentiment_batch_size)  # fmt: skip
        social.score_pending(news_config, stocks, scorer)

    return [
        ("ValuePickr collection", collect),
        ("social linking", lambda: social.link(stocks, social_config)),
        ("social sentiment", score),
        ("daily social aggregates", lambda: social.rebuild_daily(news_config)),
    ]


def run_steps(
    label: str, make_steps: Callable[[], list[tuple[str, Callable[[], None]]]]
) -> list[str]:
    """Run a pipeline's steps, isolating failures. Returns the names of failed steps."""
    try:
        steps = make_steps()
    except Exception:
        logger.exception("Could not set up the %s pipeline", label)
        return [f"{label} setup"]
    failed = []
    for i, (name, step) in enumerate(steps, 1):
        logger.info("%s %d/%d: %s", label.capitalize(), i, len(steps), name)
        try:
            step()
        except Exception as exc:
            if isinstance(exc, StepError):
                logger.error("%s: %s", name, exc)
            else:
                logger.exception("%s failed", name)
            failed.append(name)
    return failed


def run_news(stocks: list[Stock]) -> list[str]:
    """Run every news step, isolating failures. Returns the names of failed steps."""
    return run_steps("news", lambda: news_steps(stocks))


def run_filings(stocks: list[Stock]) -> list[str]:
    """Run every filings step, isolating failures. Returns the names of failed steps."""
    return run_steps("filings", lambda: filings_steps(stocks))


def run_social(stocks: list[Stock]) -> list[str]:
    """Run every social step, isolating failures. Returns the names of failed steps."""
    return run_steps("social", lambda: social_steps(stocks))


def run_alerts(stocks: list[Stock]) -> None:
    """Build and store the latest completed session's alerts (lazy import, as for news)."""
    from config.market_calendar import latest_completed_session, load_holidays
    from processing import alerts

    day = latest_completed_session(dt.datetime.now(dt.UTC), load_holidays())
    alerts.run(stocks, [day])


TELEGRAM_DELIVERED = "# telegram: delivered"


def run_backup() -> None:
    """Back up the database and data/filings/ into BACKUP_DIR (lazy import)."""
    from storage import backup

    backup.run_backup()


def run_delivery(stocks: list[Stock], failed: list[str]) -> bool:
    """Send alerts and the digest to Telegram (lazy import). True if Telegram delivered;
    False if it isn't configured. Raises if sending failed."""
    from config.market_calendar import latest_completed_session, load_holidays
    from delivery import telegram

    day = latest_completed_session(dt.datetime.now(dt.UTC), load_holidays())
    return telegram.deliver_day(day, stocks, failed) is not None


def record_run(started_at: dt.datetime, failed: list[str]) -> None:
    """Store this run in pipeline_runs (never raises: a logging problem mustn't hide results)."""
    try:
        record_pipeline_run(
            {
                "started_at": started_at,
                "finished_at": dt.datetime.now(dt.UTC),
                "exit_code": 1 if failed else 0,
                "failures": "".join(f"{name}\n" for name in failed),
            }
        )
    except Exception:
        logger.exception("Could not record the run in pipeline_runs")


def check_session_bars(stocks: Sequence[PriceSeries], already_failed: set[str]) -> list[str]:
    """Stocks missing the latest session's bar, excluding ones whose download failed."""
    try:
        missing = missing_session_bars(stocks)
    except Exception:
        logger.exception("Missing-bar check failed")
        return ["missing-bar check"]
    return sorted(set(missing) - already_failed)


def run(
    full_indicators: bool = False,
    skip_news: bool = False,
    skip_filings: bool = False,
    skip_social: bool = False,
    skip_backup: bool = False,
    failures_file: Path | None = None,
) -> int:
    """Run the full update pipeline and return a process exit code."""
    started = time.monotonic()
    started_at = dt.datetime.now(dt.UTC)
    init_db()
    stocks = load_watchlist()
    series = [*stocks, *load_benchmarks()]

    logger.info("Step 1/5: collecting prices for %d stock(s) and %d benchmark(s)",
                len(stocks), len(series) - len(stocks))  # fmt: skip
    price_summary = collect_all(series)
    log_summary(price_summary)
    missing_bars = check_session_bars(series, set(price_summary.failures))

    logger.info("Step 2/5: computing indicators%s and signals",
                " (full recompute)" if full_indicators else "")  # fmt: skip
    changed_actions = sync_actions_from_config()
    indicator_failures = process_all(
        [s.symbol for s in series], full=full_indicators, force_full=changed_actions
    )
    signal_failures = compute_signals(stocks)

    news_failures: list[str] = []
    if skip_news:
        logger.info("Step 3/5: news skipped (--skip-news)")
    else:
        logger.info("Step 3/5: news pipeline")
        news_failures = run_news(stocks)

    filings_failures: list[str] = []
    if skip_filings:
        logger.info("Step 4/5: filings skipped (--skip-filings)")
    else:
        logger.info("Step 4/5: filings pipeline")
        filings_failures = run_filings(stocks)

    social_failures: list[str] = []
    if skip_social:
        logger.info("Step 5/5: social skipped (--skip-social)")
    else:
        logger.info("Step 5/5: social pipeline")
        social_failures = run_social(stocks)

    failed = [f"prices: {symbol}" for symbol in sorted(price_summary.failures)]
    failed += [f"missing bar: {symbol}" for symbol in missing_bars]
    failed += [f"indicators: {symbol}" for symbol in sorted(indicator_failures)]
    failed += [f"signals: {symbol}" for symbol in sorted(signal_failures)]
    failed += [f"news: {name}" for name in news_failures]
    failed += [f"filings: {name}" for name in filings_failures]
    failed += [f"social: {name}" for name in social_failures]
    record_run(started_at, failed)
    try:
        run_alerts(stocks)
    except Exception:
        logger.exception("Building alerts failed")
        failed.append("alerts")
        record_run(started_at, failed)
    if not skip_backup:
        try:
            run_backup()
        except Exception:
            logger.exception("Backup failed")
            failed.append("backup")
            record_run(started_at, failed)
    delivered = False
    try:
        delivered = run_delivery(stocks, failed)
    except Exception:
        logger.exception("Telegram delivery failed")
        failed.append("telegram delivery")
        record_run(started_at, failed)
    elapsed = time.monotonic() - started
    if failures_file is not None:
        lines = [*failed, *([TELEGRAM_DELIVERED] if delivered else [])]
        failures_file.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    if failed:
        logger.error("Update finished in %.1fs with failures: %s", elapsed, ", ".join(failed))
        return 1
    logger.info("Update finished successfully in %.1fs", elapsed)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, configure logging and run the update."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--full-indicators",
        action="store_true",
        help="recompute indicators over all history instead of incrementally",
    )
    parser.add_argument("--skip-news", action="store_true", help="skip the news pipeline")
    parser.add_argument("--skip-filings", action="store_true", help="skip the filings pipeline")
    parser.add_argument("--skip-social", action="store_true", help="skip the social pipeline")
    parser.add_argument("--skip-backup", action="store_true", help="skip the data backup")
    parser.add_argument(
        "--failures-file",
        type=Path,
        help="write the names of failed steps here, one per line (empty on success)",
    )
    args = parser.parse_args(argv)
    setup_logging()
    return run(
        full_indicators=args.full_indicators,
        skip_news=args.skip_news,
        skip_filings=args.skip_filings,
        skip_social=args.skip_social,
        skip_backup=args.skip_backup,
        failures_file=args.failures_file,
    )


if __name__ == "__main__":
    sys.exit(main())
