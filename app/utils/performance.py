"""Performance utilities — timing, caching, batch processing."""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


def timed(func: Callable) -> Callable:
    """Decorator to log execution time of async functions."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.monotonic()
        result = await func(*args, **kwargs)
        elapsed = (time.monotonic() - start) * 1000
        logger.debug(f"{func.__qualname__}: {elapsed:.0f}ms")
        return result
    return wrapper


def timed_sync(func: Callable) -> Callable:
    """Decorator to log execution time of sync functions."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        result = func(*args, **kwargs)
        elapsed = (time.monotonic() - start) * 1000
        logger.debug(f"{func.__qualname__}: {elapsed:.0f}ms")
        return result
    return wrapper


async def process_batch(items: list, processor, batch_size: int = 5) -> list:
    """Process items in batches to avoid overwhelming resources."""
    import asyncio
    results = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batch_results = await asyncio.gather(
            *[processor(item) for item in batch],
            return_exceptions=True,
        )
        for r in batch_results:
            if isinstance(r, Exception):
                logger.error(f"Batch item failed: {r}")
                results.append(None)
            else:
                results.append(r)
    return results


class Timer:
    """Context manager for timing code blocks."""

    def __init__(self, name: str = ""):
        self.name = name
        self.elapsed_ms = 0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = int((time.monotonic() - self._start) * 1000)
        if self.name:
            logger.debug(f"{self.name}: {self.elapsed_ms}ms")
