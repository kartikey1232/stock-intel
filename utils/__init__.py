"""Shared utilities."""

import warnings

from utils.logging_setup import get_logger, setup_logging

# pandas-ta 0.4.x sets a pandas option that pandas 3 deprecates; harmless, so silence it.
warnings.filterwarnings("ignore", message=".*copy_on_write.*")

__all__ = ["get_logger", "setup_logging"]
