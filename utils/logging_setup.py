"""Shared logging configuration: console output plus a rotating file in logs/."""

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
LOG_FORMAT = "%(asctime)s UTC | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_HANDLER_MARKER = "_stock_intel_handler"


def setup_logging(
    level: int | str = logging.INFO,
    log_dir: Path = DEFAULT_LOG_DIR,
    log_file: str = "stock_intel.log",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure the root logger to write to the console and a rotating file.

    Safe to call more than once: handlers added by a previous call are replaced,
    so log lines are never duplicated. Timestamps are written in UTC.

    Args:
        level: Minimum level for the root logger (e.g. logging.INFO or "DEBUG").
        log_dir: Directory for log files; created if missing.
        log_file: Name of the log file inside log_dir.
        max_bytes: Size at which the log file is rotated.
        backup_count: Number of rotated files to keep.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    formatter.converter = time.gmtime

    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        log_dir / log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]:
        root.removeHandler(handler)
        handler.close()

    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)

    root.setLevel(level)
    # Third-party libraries are noisy at INFO/DEBUG.
    for noisy in ("urllib3", "httpx", "httpcore", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call setup_logging() once at application start."""
    return logging.getLogger(name)
