"""Composition root: wires concrete adapters to protocols (Global Constraint 4)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from research_engine import __version__
from research_engine.adapters.crawl4ai import Crawl4AIFetcher
from research_engine.adapters.html_extract import DefaultHtmlExtractor
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.adapters.searxng import SearxngProvider
from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.api import fetch as fetch_api
from research_engine.api import search as search_api
from research_engine.api.auth import ApiKeyMiddleware
from research_engine.api.deps import Services
from research_engine.api.envelope import (
    RequestContextMiddleware,
    UnhandledErrorMiddleware,
    install_exception_handlers,
)
from research_engine.cache.base import Cache
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings, get_settings
from research_engine.config_files import IntentRegistry
from research_engine.events.base import EventSink
from research_engine.events.memory import InMemoryEventBus
from research_engine.logging import configure_logging
from research_engine.pipeline.fetch import FetchService
from research_engine.pipeline.search import SearchService
from research_engine.safety.http import make_fetch_client
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import RobotsPolicy
from research_engine.safety.ssrf import SsrfGuard


def build_fetch_service(
    settings: Settings,
    *,
    http: httpx.AsyncClient,
    fetch_http: httpx.AsyncClient,
    cache: Cache,
    events: EventSink,
) -> FetchService:
    """``fetch_http`` (cookie-less, no redirects) talks to third-party sites; ``http`` only to
    the internal Crawl4AI service."""
    guard = SsrfGuard(settings.ssrf_allow_hosts_set)
    limiter = DomainLimiter(settings.domain_concurrency, settings.domain_delay_s)
    robots = RobotsPolicy(fetch_http, settings.user_agent, limiter, guard)
    static = StaticFetcher(
        fetch_http,
        guard,
        max_bytes=settings.max_response_bytes,
        allowed_types=settings.allowed_content_types_set,
        user_agent=settings.user_agent,
    )
    browser = Crawl4AIFetcher(
        settings.crawl4ai_url, settings.crawl4ai_api_token.get_secret_value(), http, guard
    )
    return FetchService(
        static,
        browser,
        DefaultHtmlExtractor(),
        PypdfExtractor(),
        robots,
        limiter,
        cache,
        events,
        settings,
    )


def build_services(settings: Settings) -> Services:
    http = httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, follow_redirects=False)
    fetch_http = make_fetch_client(settings.user_agent)
    intents = IntentRegistry.load(settings.intents_file)
    events = InMemoryEventBus()
    cache = InMemoryCache()
    provider = SearxngProvider(settings.searxng_url, http, settings.search_timeout_s)
    return Services(
        settings=settings,
        intents=intents,
        events=events,
        cache=cache,
        search=SearchService(provider, intents, cache, events, settings),
        fetch=build_fetch_service(
            settings, http=http, fetch_http=fetch_http, cache=cache, events=events
        ),
        http=http,
        fetch_http=fetch_http,
    )


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = services is None
        app.state.services = build_services(settings) if services is None else services
        try:
            yield
        finally:
            if owned:
                for client in (app.state.services.http, app.state.services.fetch_http):
                    if client is not None:
                        await client.aclose()

    app = FastAPI(title="Research Engine", version=__version__, lifespan=lifespan)
    install_exception_handlers(app)
    app.include_router(search_api.router)
    app.include_router(fetch_api.router)
    # Middleware order: last added is outermost. RequestContext must wrap everything (so even
    # the 401 carries a request_id), then the catch-all, then auth.
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
