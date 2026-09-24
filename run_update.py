"""Daily update: prices, indicators, news, filings and social posts for the watchlist.

Steps:
  1. prices       collect daily OHLCV (collectors.prices), then check every stock has a
                  bar for the latest completed trading session
  2. indicators   sync corporate actions, compute indicators (processing.indicators)
  3. news         collect articles -> article text -> story grouping -> entity linking
                  -> sentiment -> daily aggregates (skip with --skip-news)
  4. filings      HDFC Bank results PDFs from its IR site -> import the results inbox
                  (data/filings/inbox/) -> classify filings -> extract results
                  (skip with --skip-filings). There is no exchange filings collector:
                  NSE/BSE access is undecided (see README).
  5. social       ValuePickr posts -> stock links -> sentiment -> daily aggregates
                  (skip with --skip-social)

Corporate actions are synced from config/corporate_actions.yaml before indicators are
computed; symbols whose actions changed get a full indicator recompute.

Every step runs even if an earlier one failed: indicators run for symbols whose price
download failed (they keep their previous data), and news runs after price problems.
Each news, filings and social step is isolated the same way, so a failing feed, file or model
never blocks the rest. Exit code is 0 on full success, 1 if anything failed.
--failures-file writes the failed steps one per line (empty on success), for the
scheduled wrapper's notification.

Run with:  uv run python run_update.py [--full-indicators] [--skip-news] [--skip-filings]
                                       [--skip-social] [--failures-file PATH]
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from collectors.prices import collect_all, log_summary, missing_session_bars
from config.loader import Stock, load_watchlist
from processing.adjustments import sync_actions_from_config
from processing.indicators import process_all
from storage.db import init_db
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


def check_session_bars(stocks: list[Stock], already_failed: set[str]) -> list[str]:
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
    failures_file: Path | None = None,
) -> int:
    """Run the full update pipeline and return a process exit code."""
    started = time.monotonic()
    init_db()
    stocks = load_watchlist()

    logger.info("Step 1/5: collecting prices for %d stock(s)", len(stocks))
    price_summary = collect_all(stocks)
    log_summary(price_summary)
    missing_bars = check_session_bars(stocks, set(price_summary.failures))

    logger.info("Step 2/5: computing indicators%s", " (full recompute)" if full_indicators else "")
    changed_actions = sync_actions_from_config()
    indicator_failures = process_all(
        [s.symbol for s in stocks], full=full_indicators, force_full=changed_actions
    )

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
    failed += [f"news: {name}" for name in news_failures]
    failed += [f"filings: {name}" for name in filings_failures]
    failed += [f"social: {name}" for name in social_failures]
    elapsed = time.monotonic() - started
    if failures_file is not None:
        failures_file.write_text("".join(f"{name}\n" for name in failed), encoding="utf-8")
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
        failures_file=args.failures_file,
    )


if __name__ == "__main__":
    sys.exit(main())
