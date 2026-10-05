"""Composition root: wires concrete adapters to protocols (Global Constraint 4)."""

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from research_engine_client.models import Event, EventKind, EventLevel, JobType

from research_engine import __version__
from research_engine.adapters.crawl4ai import Crawl4AIFetcher
from research_engine.adapters.html_extract import DefaultHtmlExtractor
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.adapters.searxng import SearxngProvider
from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.api import fetch as fetch_api
from research_engine.api import health as health_api
from research_engine.api import jobs as jobs_api
from research_engine.api import jobs_submit as jobs_submit_api
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
from research_engine.gui import routes as gui_routes
from research_engine.gui.routes import GUI_DIR, SecurityHeadersMiddleware
from research_engine.gui.session import LoginRateLimiter, SessionCodec
from research_engine.jobs.runner import JobRunner
from research_engine.jobs.store import JobStore
from research_engine.logging import configure_logging
from research_engine.maintenance import maintenance_loop
from research_engine.mcp import build_mcp, mount_mcp
from research_engine.pipeline.fetch import FetchService
from research_engine.pipeline.search import SearchService
from research_engine.pipeline.search_read import run_batch_fetch, run_search_read
from research_engine.safety.http import make_fetch_client
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import RobotsPolicy
from research_engine.safety.ssrf import SsrfGuard
from research_engine.shutdown import begin_shutdown, shutdown_signals
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


def register_job_handlers(
    runner: JobRunner, settings: Settings, search: SearchService, fetch: FetchService
) -> None:
    """Register every job type's handler (shared by the real and the test composition roots)."""
    runner.register(
        JobType.FETCH_BATCH,
        lambda ctx: run_batch_fetch(ctx, fetch, concurrency=settings.job_fetch_concurrency),
    )
    runner.register(
        JobType.SEARCH_READ,
        lambda ctx: run_search_read(ctx, search, fetch, concurrency=settings.job_fetch_concurrency),
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
    fetch = build_fetch_service(
        settings, http=http, fetch_http=fetch_http, cache=cache, events=events
    )
    search = SearchService(provider, intents, cache, events, settings)
    register_job_handlers(runner, settings, search, fetch)
    return Services(
        settings=settings,
        intents=intents,
        events=events,
        cache=cache,
        search=search,
        fetch=fetch,
        engine=engine,
        jobs=runner,
        job_store=job_store,
        health_checks={"searxng": provider.health, "crawl4ai": fetch.browser_health},
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


_SQLITE_JOIN_TIMEOUT_S = 2.0


def _sqlite_worker_threads() -> list[threading.Thread]:
    """Live aiosqlite worker threads. ``aiosqlite.Connection`` starts
    ``Thread(target=_connection_worker_thread, ...)`` without a name, so Python names it
    ``Thread-N (_connection_worker_thread)`` (3.10+, from the target's ``__name__``). ``_target``
    cannot be used: ``Thread.run`` deletes it once the thread has started."""
    return [t for t in threading.enumerate() if "_connection_worker_thread" in t.name]


def _join_threads(threads: list[threading.Thread], timeout_s: float) -> list[threading.Thread]:
    """Blocking: join with one shared deadline. Returns the threads still alive."""
    deadline = time.monotonic() + timeout_s
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [t for t in threads if t.is_alive()]


async def _join_sqlite_workers(timeout_s: float = _SQLITE_JOIN_TIMEOUT_S) -> None:
    """Wait (bounded, off the event loop) for aiosqlite's worker threads to exit.

    SQLAlchemy forces those workers to ``daemon=True`` and ``engine.dispose()`` returns as soon
    as ``close()``'s future resolves, while the worker still has bytecode to run. A daemon thread
    still running at interpreter finalisation is a known CPython crash class
    (python/cpython#124878, #140257), so join them. ``timeout_s`` is the *total* budget; a thread
    still alive after it is logged and abandoned, so shutdown can never hang.
    """
    threads = _sqlite_worker_threads()
    if not threads:
        return
    stuck = await asyncio.to_thread(_join_threads, threads, timeout_s)
    if stuck:
        _log.warning(
            "aiosqlite worker threads still alive after join timeout",
            threads=[t.name for t in stuck],
            timeout_s=timeout_s,
        )


async def _shutdown(
    services: Services,
    maintenance: asyncio.Task[None] | None,
    *,
    close_http: bool,
    started: bool,
    mcp: AsyncExitStack | None = None,
) -> None:
    """Reverse of startup. Every step runs even if an earlier one raises. After a clean
    startup the first error is re-raised afterwards (later ones are logged); after a failed
    startup ``system.shutdown`` is not emitted and cleanup errors are only logged, so the
    original startup error is the one that propagates.

    The shutdown budget is additive. On SIGTERM uvicorn first waits up to
    ``--timeout-graceful-shutdown`` (5 s, step 8) for open connections, then runs this lifespan
    shutdown, which has no timeout (the runner's worst-case ``stop()`` is about 9 s). So the
    container's ``stop_grace_period`` (30 s) must exceed graceful wait + runner stop + slack.
    The SIGTERM hook installed in the lifespan (``research_engine.shutdown``) ends SSE streams
    and wakes ``?wait=`` long-polls the moment the signal arrives, so in practice the graceful
    wait is short; the 5 s is only the cap for other in-flight requests.
    """
    steps: list[tuple[str, Callable[[], Awaitable[object]]]] = []
    if started:
        steps.append(
            (
                "emit system.shutdown",
                lambda: _emit_system(services, EventKind.SYSTEM_SHUTDOWN, "service stopping"),
            )
        )
    if mcp is not None:
        # Started last, stopped first: cancels any MCP request still in flight before the
        # services it uses go away.
        steps.append(("stop mcp session manager", mcp.aclose))
    steps += [
        ("stop maintenance", lambda: _cancel_task(maintenance)),
        ("stop job runner", services.jobs.stop),
    ]
    if close_http:
        steps.append(("close http clients", lambda: _close_http(services)))
    steps.append(("dispose engine", services.engine.dispose))
    steps.append(("join aiosqlite workers", _join_sqlite_workers))
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
        mcp_stack = AsyncExitStack()
        started = False
        try:
            await init_db(svc.engine)
            await svc.jobs.start()
            await _emit_system(svc, EventKind.SYSTEM_STARTUP, "service started")
            maintenance = asyncio.create_task(maintenance_loop(svc), name="maintenance")
            # The MCP transport's task group (V1-12); a failure entering it is a startup failure.
            await mcp_stack.enter_async_context(app.state.mcp.session_manager.run())
            started = True
            app.state.shutting_down = False
            # SIGTERM/SIGINT end SSE streams and long-polls at once (see research_engine.shutdown).
            with shutdown_signals(lambda: begin_shutdown(app)):
                yield
        finally:
            await _shutdown(svc, maintenance, close_http=owned, started=started, mcp=mcp_stack)

    # No /docs or /redoc: they run CDN JavaScript without a CSP on the origin holding the GUI
    # session cookie. /openapi.json stays (and stays open).
    app = FastAPI(
        title="Research Engine",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    install_exception_handlers(app)
    app.include_router(search_api.router)
    app.include_router(fetch_api.router)
    app.include_router(jobs_api.router)
    app.include_router(jobs_submit_api.router)
    app.include_router(health_api.router)
    app.include_router(health_api.schemas_router)
    app.include_router(gui_routes.router)
    app.mount("/static", StaticFiles(directory=GUI_DIR / "static"), name="static")
    # MCP (V1-12) at /mcp, behind ApiKeyMiddleware like /v1. Built per app: its session
    # manager runs once, in this app's lifespan.
    mcp = build_mcp(lambda: app.state.services)
    mount_mcp(
        app, mcp, allowed_hosts=[settings.site_host, "localhost", "127.0.0.1", "research.localhost"]
    )
    app.state.mcp = mcp
    api_key = settings.api_key.get_secret_value()
    codec = SessionCodec(settings.session_secret.get_secret_value())
    app.state.api_key = api_key.encode()
    app.state.session_codec = codec
    app.state.site_host = settings.site_host
    app.state.login_limiter = LoginRateLimiter()
    # Middleware order: last added is outermost. RequestContext must wrap everything (so even
    # the 401 carries a request_id), then the security headers (so GUI redirects, 403s and
    # 500s carry the CSP), then the catch-all, then auth.
    app.add_middleware(ApiKeyMiddleware, api_key=api_key, codec=codec, site_host=settings.site_host)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
