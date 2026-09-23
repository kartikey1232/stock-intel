import logging
from pathlib import Path

from utils.logging_setup import setup_logging


def test_writes_to_rotating_file(tmp_path: Path) -> None:
    setup_logging(log_dir=tmp_path, log_file="test.log")
    logging.getLogger("tests.sample").info("hello from test")
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = (tmp_path / "test.log").read_text(encoding="utf-8")
    assert "hello from test" in content
    assert "UTC" in content


def test_repeated_setup_does_not_duplicate_handlers(tmp_path: Path) -> None:
    setup_logging(log_dir=tmp_path)
    count = len(logging.getLogger().handlers)
    setup_logging(log_dir=tmp_path)
    assert len(logging.getLogger().handlers) == count
