"""Periodic housekeeping: event retention and cache expiry (V1-10, V1-15)."""

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from research_engine.api.deps import Services

_log = structlog.get_logger("research_engine.maintenance")
INTERVAL_S = 3600


async def run_once(services: Services) -> dict[str, int]:
    """One pass; each store opens and closes its own session (``:memory:``-safe)."""
    events = await services.events.prune(services.settings.event_retention_days)
    cache = await services.cache.prune()
    return {"events_pruned": events, "cache_pruned": cache}


async def maintenance_loop(
    services: Services,
    *,
    interval_s: float = INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run ``run_once`` forever. Any ``Exception`` is logged and the loop carries on;
    cancellation (``BaseException``) ends it."""
    while True:
        try:
            _log.info("maintenance", **await run_once(services))
        except Exception:
            _log.exception("maintenance pass failed")
        await sleep(interval_s)
