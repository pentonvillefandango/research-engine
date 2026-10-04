"""Composition root: wires concrete adapters to protocols (Global Constraint 4)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from research_engine import __version__
from research_engine.adapters.searxng import SearxngProvider
from research_engine.api import search as search_api
from research_engine.api.auth import ApiKeyMiddleware
from research_engine.api.deps import Services
from research_engine.api.envelope import (
    RequestContextMiddleware,
    UnhandledErrorMiddleware,
    install_exception_handlers,
)
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings, get_settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.logging import configure_logging
from research_engine.pipeline.search import SearchService


def build_services(settings: Settings) -> Services:
    http = httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, follow_redirects=False)
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
        http=http,
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
            if owned and app.state.services.http is not None:
                await app.state.services.http.aclose()

    app = FastAPI(title="Research Engine", version=__version__, lifespan=lifespan)
    install_exception_handlers(app)
    app.include_router(search_api.router)
    # Middleware order: last added is outermost. RequestContext must wrap everything (so even
    # the 401 carries a request_id), then the catch-all, then auth.
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
