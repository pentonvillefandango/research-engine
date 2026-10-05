"""Service container hung on app.state (composition root fills it)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from research_engine.cache.base import Cache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.base import EventStore
from research_engine.jobs.runner import JobRunner
from research_engine.jobs.store import JobStore
from research_engine.pipeline.fetch import FetchService
from research_engine.pipeline.search import SearchService

HEALTH_TIMEOUT_S = 5.0
HEALTH_TTL_S = 5.0


@dataclass
class Services:
    settings: Settings
    intents: IntentRegistry
    events: EventStore
    cache: Cache
    search: SearchService
    fetch: FetchService
    engine: AsyncEngine
    jobs: JobRunner
    job_store: JobStore
    health_checks: dict[str, Callable[[], Awaitable[bool]]]
    """Name -> adapter ``health`` callable (searxng, crawl4ai); wired in the composition root."""
    http: httpx.AsyncClient | None = None
    """App-wide client for internal services (SearXNG, Crawl4AI)."""
    fetch_http: httpx.AsyncClient | None = None
    """Cookie-less, no-redirect client for third-party pages (robots + static fetcher)."""
    health_timeout_s: float = HEALTH_TIMEOUT_S
    health_ttl_s: float = HEALTH_TTL_S
    """How long ``/health`` reuses a dependency check (it is unauthenticated and polled)."""
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


def get_services(request: Request) -> Services:
    return request.app.state.services
