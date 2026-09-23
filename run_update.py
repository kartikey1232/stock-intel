"""Daily update: prices and indicators, then the news pipeline, for the whole watchlist.

Steps:
  1. prices       collect daily OHLCV (collectors.prices)
  2. indicators   sync corporate actions, compute indicators (processing.indicators)
  3. news         collect articles -> article text -> story grouping -> entity linking
                  -> sentiment -> daily aggregates (skip with --skip-news)

Corporate actions are synced from config/corporate_actions.yaml before indicators are
computed; symbols whose actions changed get a full indicator recompute.

Every step runs even if an earlier one failed: indicators run for symbols whose price
download failed (they keep their previous data), and news runs after price problems.
Each news step is isolated the same way, so a failing feed or model never blocks the
rest. Exit code is 0 on full success, 1 if anything failed.

Run with:  uv run python run_update.py [--full-indicators] [--skip-news]
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable

from collectors.prices import collect_all, log_summary
from config.loader import Stock, load_watchlist
from processing.adjustments import sync_actions_from_config
from processing.indicators import process_all
from storage.db import init_db
from utils import setup_logging

logger = logging.getLogger("run_update")


class NewsStepError(RuntimeError):
    """A news step finished but reported failures (e.g. some feeds failed)."""


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
            raise NewsStepError(f"{len(failed)} source(s) failed: {', '.join(failed)}")

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


def run_news(stocks: list[Stock]) -> list[str]:
    """Run every news step, isolating failures. Returns the names of failed steps."""
    try:
        steps = news_steps(stocks)
    except Exception:
        logger.exception("Could not set up the news pipeline")
        return ["news setup"]
    failed = []
    for i, (name, step) in enumerate(steps, 1):
        logger.info("News %d/%d: %s", i, len(steps), name)
        try:
            step()
        except Exception as exc:
            if isinstance(exc, NewsStepError):
                logger.error("%s: %s", name, exc)
            else:
                logger.exception("%s failed", name)
            failed.append(name)
    return failed


def run(full_indicators: bool = False, skip_news: bool = False) -> int:
    """Run the full update pipeline and return a process exit code."""
    started = time.monotonic()
    init_db()
    stocks = load_watchlist()

    logger.info("Step 1/3: collecting prices for %d stock(s)", len(stocks))
    price_summary = collect_all(stocks)
    log_summary(price_summary)

    logger.info("Step 2/3: computing indicators%s", " (full recompute)" if full_indicators else "")
    changed_actions = sync_actions_from_config()
    indicator_failures = process_all(
        [s.symbol for s in stocks], full=full_indicators, force_full=changed_actions
    )

    news_failures: list[str] = []
    if skip_news:
        logger.info("Step 3/3: news skipped (--skip-news)")
    else:
        logger.info("Step 3/3: news pipeline")
        news_failures = run_news(stocks)

    failed = sorted(set(price_summary.failures) | set(indicator_failures))
    failed += [f"news: {name}" for name in news_failures]
    elapsed = time.monotonic() - started
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
    parser.add_argument("--skip-news", action="store_true", help="only prices and indicators")
    args = parser.parse_args(argv)
    setup_logging()
    return run(full_indicators=args.full_indicators, skip_news=args.skip_news)


if __name__ == "__main__":
    sys.exit(main())
