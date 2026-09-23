"""Daily update: collect prices, then recompute indicators, for the whole watchlist.

Corporate actions are synced from config/corporate_actions.yaml before indicators are
computed; symbols whose actions changed get a full indicator recompute.

Indicators run for every symbol even if some price downloads failed: symbols that
failed simply keep their previous data, and recomputing them is harmless.

Run with:  uv run python run_update.py [--full-indicators]
Exit code is 0 on full success, 1 if any symbol failed in either step.
"""

import argparse
import logging
import sys
import time

from collectors.prices import collect_all, log_summary
from config.loader import load_watchlist
from processing.adjustments import sync_actions_from_config
from processing.indicators import process_all
from storage.db import init_db
from utils import setup_logging

logger = logging.getLogger("run_update")


def run(full_indicators: bool = False) -> int:
    """Run the full update pipeline and return a process exit code."""
    started = time.monotonic()
    init_db()
    stocks = load_watchlist()

    logger.info("Step 1/2: collecting prices for %d stock(s)", len(stocks))
    price_summary = collect_all(stocks)
    log_summary(price_summary)

    logger.info("Step 2/2: computing indicators%s", " (full recompute)" if full_indicators else "")
    changed_actions = sync_actions_from_config()
    indicator_failures = process_all(
        [s.symbol for s in stocks], full=full_indicators, force_full=changed_actions
    )

    failed = sorted(set(price_summary.failures) | set(indicator_failures))
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
    args = parser.parse_args(argv)
    setup_logging()
    return run(full_indicators=args.full_indicators)


if __name__ == "__main__":
    sys.exit(main())
