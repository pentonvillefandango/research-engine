"""Composition root: wires concrete adapters to protocols (Global Constraint 4)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import FastAPI
from research_engine_client.models import Event, EventKind, EventLevel

from research_engine import __version__
from research_engine.adapters.crawl4ai import Crawl4AIFetcher
from research_engine.adapters.html_extract import DefaultHtmlExtractor
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.adapters.searxng import SearxngProvider
from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.api import fetch as fetch_api
from research_engine.api import jobs as jobs_api
from research_engine.api import search as search_api
from research_engine.api.auth import ApiKeyMiddleware
from research_engine.api.deps import Services
from research_engine.api.envelope import (
    RequestContextMiddleware,
    UnhandledErrorMiddleware,
    install_exception_handlers,
)
from research_engine.cache.base import Cache
from research_engine.cache.sqlite import SqliteCache
from research_engine.config import Settings, get_settings
from research_engine.config_files import IntentRegistry
from research_engine.events.base import EventSink
from research_engine.events.sqlite import SqliteEventBus
from research_engine.jobs.runner import JobRunner
from research_engine.jobs.store import JobStore
from research_engine.logging import configure_logging
from research_engine.maintenance import maintenance_loop
from research_engine.pipeline.fetch import FetchService
from research_engine.pipeline.search import SearchService
from research_engine.safety.http import make_fetch_client
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import RobotsPolicy
from research_engine.safety.ssrf import SsrfGuard
from research_engine.store.db import create_engine_for, init_db

_log = structlog.get_logger("research_engine.app")


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
    engine = create_engine_for(settings.db_path)
    events = SqliteEventBus(engine)
    cache = SqliteCache(engine)
    job_store = JobStore(engine)
    runner = JobRunner(
        job_store, events, workers=settings.job_workers, timeout_s=settings.job_timeout_s
    )
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
        engine=engine,
        jobs=runner,
        job_store=job_store,
        http=http,
        fetch_http=fetch_http,
    )


async def _emit_system(services: Services, kind: EventKind, message: str) -> None:
    await services.events.emit(
        Event(
            ts=datetime.now(UTC),
            level=EventLevel.INFO,
            kind=kind,
            message=message,
            data={"version": __version__, "git_sha": services.settings.git_sha},
        )
    )


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _close_http(services: Services) -> None:
    for client in (services.http, services.fetch_http):
        if client is not None:
            await client.aclose()


async def _shutdown(
    services: Services,
    maintenance: asyncio.Task[None] | None,
    *,
    close_http: bool,
    started: bool,
) -> None:
    """Reverse of startup. Every step runs even if an earlier one raises. After a clean
    startup the first error is re-raised afterwards (later ones are logged); after a failed
    startup ``system.shutdown`` is not emitted and cleanup errors are only logged, so the
    original startup error is the one that propagates.

    Deployments must set uvicorn ``--timeout-graceful-shutdown`` >= 15 s (see step 8): it must
    cover the runner's worst-case ``stop()`` time of about 9 s.
    """
    steps: list[tuple[str, Callable[[], Awaitable[object]]]] = []
    if started:
        steps.append(
            (
                "emit system.shutdown",
                lambda: _emit_system(services, EventKind.SYSTEM_SHUTDOWN, "service stopping"),
            )
        )
    steps += [
        ("stop maintenance", lambda: _cancel_task(maintenance)),
        ("stop job runner", services.jobs.stop),
    ]
    if close_http:
        steps.append(("close http clients", lambda: _close_http(services)))
    steps.append(("dispose engine", services.engine.dispose))
    first: Exception | None = None
    for name, step in steps:
        try:
            await step()
        except Exception as exc:
            _log.exception("shutdown step failed", step=name)
            first = first or exc
    if first is not None and started:
        raise first


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = services is None
        svc = app.state.services = build_services(settings) if services is None else services
        maintenance: asyncio.Task[None] | None = None
        started = False
        try:
            await init_db(svc.engine)
            await svc.jobs.start()
            await _emit_system(svc, EventKind.SYSTEM_STARTUP, "service started")
            maintenance = asyncio.create_task(maintenance_loop(svc), name="maintenance")
            started = True
            yield
        finally:
            await _shutdown(svc, maintenance, close_http=owned, started=started)

    app = FastAPI(title="Research Engine", version=__version__, lifespan=lifespan)
    install_exception_handlers(app)
    app.include_router(search_api.router)
    app.include_router(fetch_api.router)
    app.include_router(jobs_api.router)
    # Middleware order: last added is outermost. RequestContext must wrap everything (so even
    # the 401 carries a request_id), then the catch-all, then auth.
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
