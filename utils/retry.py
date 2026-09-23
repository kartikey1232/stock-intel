"""Retry with exponential backoff for flaky network calls."""

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

logger = logging.getLogger(__name__)


def retry(
    attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry the decorated function on `exceptions` with exponential backoff and jitter.

    Delay before retry n (1-based) is min(max_delay, base_delay * 2**(n-1)) plus up to
    25% random jitter. The last exception is re-raised once all attempts are used.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == attempts:
                        raise
                    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
                    delay += random.uniform(0, delay * 0.25)
                    logger.warning(
                        "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                        func.__qualname__,
                        attempt,
                        attempts,
                        exc,
                        delay,
                    )
                    sleep(delay)
            raise AssertionError("unreachable")

        return wrapper

    return decorator
