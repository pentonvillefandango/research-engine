"""Retry with exponential backoff for retryable ServiceErrors (§8 Reliability)."""

import asyncio
import random
from collections.abc import Awaitable, Callable

from research_engine.errors import ServiceError


async def retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_s: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``fn`` up to ``attempts`` times, retrying only retryable ServiceErrors.

    Sleeps only between attempts (never after the last). Only ServiceError is caught, so
    ``asyncio.CancelledError`` and other exceptions propagate immediately.
    """
    for i in range(attempts):
        try:
            return await fn()
        except ServiceError as exc:
            if not exc.detail.retryable or i == attempts - 1:
                raise
        await sleep(base_delay_s * 2**i * random.uniform(0.8, 1.2))  # noqa: S311 - jitter, not crypto
    raise AssertionError("unreachable")  # attempts < 1
