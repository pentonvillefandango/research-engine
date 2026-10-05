"""GUI routes (login, logout, dashboard, live event stream, partials) and the GUI
security-headers middleware (B2, V1-15, V1-16)."""

import asyncio
import json
import string
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from pydantic import BeforeValidator
from research_engine_client.models import Event, EventLevel, Job
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_engine.api import auth
from research_engine.api.auth import is_api_path
from research_engine.api.deps import Services, get_services
from research_engine.api.health import cached_dependencies, overall_state
from research_engine.gui.session import (
    COOKIE,
    MAX_AGE_S,
    LoginRateLimiter,
    SessionCodec,
    safe_next,
    same_origin,
)
from research_engine.jobs.store import to_job

GUI_DIR = Path(__file__).parent
templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(GUI_DIR / "templates"), autoescape=True)
)
router = APIRouter(include_in_schema=False)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeadersMiddleware:
    """Adds the CSP and hardening headers to every non-API HTTP response.

    Pure ASGI and header-only: it rewrites ``http.response.start`` and passes every body message
    straight through, so streaming (SSE) responses are never buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_api_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name in present:
                        continue
                    headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _client_ip(request: Request) -> str:
    # Behind Caddy, uvicorn --proxy-headers has already put the forwarded client IP here.
    return request.client.host if request.client else "unknown"


def _origin_ok(request: Request) -> bool:
    hosts = request.headers.getlist("host")
    return same_origin(
        request.headers.getlist("origin"),
        request.headers.getlist("referer"),
        request.url.scheme,
        hosts[0] if len(hosts) == 1 else "",
        request.app.state.site_host,
    )


def _secure(request: Request) -> bool:
    # Behind Caddy, uvicorn --proxy-headers maps X-Forwarded-Proto into the scheme.
    return request.url.scheme == "https"


def _login_page(request: Request, next_path: str, error: str | None, status: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_path, "error": error},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return _login_page(request, safe_next(next), None, 200)


@router.post("/login")
async def login(
    request: Request,
    api_key: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    # /login is an open path, so the CSRF Origin check the middleware applies to cookie
    # requests has to happen here (login CSRF / key-guessing from a foreign page).
    if not _origin_ok(request):
        return PlainTextResponse("403 Forbidden: cross-origin request refused\n", status_code=403)
    target = safe_next(next)
    limiter: LoginRateLimiter = request.app.state.login_limiter
    client = _client_ip(request)
    if limiter.blocked(client):
        response = _login_page(request, target, "Too many failed attempts; try again later.", 429)
        response.headers["Retry-After"] = str(limiter.retry_after(client))
        return response
    expected: bytes = request.app.state.api_key
    if not auth.key_matches(api_key, expected):
        limiter.fail(client)
        return _login_page(request, target, "Invalid key", 401)
    limiter.reset(client)
    codec: SessionCodec = request.app.state.session_codec
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        codec.issue(),
        max_age=MAX_AGE_S,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_secure(request),
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    # Not an open path: the auth middleware has already required a session (or key) and run
    # the Origin check for cookie requests.
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        COOKIE, path="/", httponly=True, samesite="strict", secure=_secure(request)
    )
    return response


# --- dashboard ---------------------------------------------------------------------------------

MAX_EVENT_STREAMS = 50
"""Concurrent ``/gui/events/stream`` connections per process; more get 503 (memory bound:
each stream holds a subscriber queue of up to 1000 events)."""
HISTORY_LIMIT = 200
"""Matching events replayed when a stream opens, before live events."""
RECENT_JOBS = 10
_STREAMS_KEY = "gui_event_streams"
_NO_STORE = {"Cache-Control": "no-store"}
_LEVEL_ORDER = {lv: i for i, lv in enumerate(EventLevel)}
# SQLite's lower()/LIKE fold ASCII only; folding the same way here keeps the live filter
# identical to the history query's.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _blank_is_none(value: object) -> object:
    # The filter form submits empty fields; an empty filter means "no filter".
    return None if value == "" else value


class _StreamSlots:
    def __init__(self) -> None:
        self.active = 0


def _stream_slots(services: Services) -> _StreamSlots:
    return services.extra.setdefault(_STREAMS_KEY, _StreamSlots())


def active_streams(services: Services) -> int:
    """Open ``/gui/events/stream`` connections in this process."""
    return _stream_slots(services).active


async def _stream_slot(services: Annotated[Services, Depends(get_services)]) -> AsyncIterator[None]:
    """Reserve one stream slot for the whole response, or 503.

    A dependency with ``yield``: FastAPI runs its exit code after the streaming response has
    finished, so the slot is held exactly as long as the stream. Check and increment happen
    with no ``await`` in between, so concurrent requests cannot overshoot the cap.
    """
    slots = _stream_slots(services)
    if slots.active >= MAX_EVENT_STREAMS:
        raise HTTPException(503, "too many open event streams", headers={"Retry-After": "10"})
    slots.active += 1
    try:
        yield
    finally:
        slots.active -= 1


_detached: set[asyncio.Task[Any]] = set()


def _forget(task: asyncio.Task[Any]) -> None:
    _detached.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved: no "exception was never retrieved" noise after a disconnect


async def _cancel_safe[T](coro: Coroutine[Any, Any, T]) -> T:
    """Await database work in its own task, shielded from the caller's cancellation.

    On client disconnect FastAPI cancels the SSE generator through an anyio cancel scope. That
    cancellation is level-triggered: after a mid-query cancel it also cancels SQLAlchemy's
    cleanup awaits (invalidate / close), so the pooled connection is never checked back in and
    the pool slot is lost. A separate task is outside that scope: the query finishes and returns
    its connection normally, and only this await is abandoned.
    """
    task = asyncio.ensure_future(coro)
    _detached.add(task)
    task.add_done_callback(_forget)
    return await asyncio.shield(task)


def _fmt_time(ts: datetime) -> str:
    ts = ts.astimezone(UTC)
    return f"{ts:%H:%M:%S}.{ts.microsecond // 1000:03d}"


def render_event_row(e: Event) -> str:
    """One ``_event_row.html`` fragment. Jinja autoescapes every field; ``data`` is shown as
    escaped, pretty-printed JSON. Newlines are kept: FastAPI splits a multi-line ``raw_data``
    into one ``data:`` line each, which ``EventSource`` joins back with ``\n``."""
    data_json = json.dumps(e.data, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    return (
        templates.env.get_template("_event_row.html")
        .render(e=e, time=_fmt_time(e.ts), data_json=data_json)
        .strip()
    )


def _log_event(e: Event) -> ServerSentEvent:
    # ``id`` equals the row's ``data-id``: the client de-duplicates on it without parsing HTML.
    return ServerSentEvent(
        event="log", id=None if e.id is None else str(e.id), raw_data=render_event_row(e)
    )


@router.get(
    "/gui/events/stream",
    response_class=EventSourceResponse,
    dependencies=[Depends(_stream_slot)],
)
async def event_stream(
    services: Annotated[Services, Depends(get_services)],
    level: Annotated[EventLevel | None, BeforeValidator(_blank_is_none), Query()] = None,
    job: Annotated[str | None, Query(max_length=64)] = None,
    kind: Annotated[str | None, Query(pattern=r"^[a-z_.]{0,64}$")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> AsyncIterator[ServerSentEvent]:
    """Matching history (newest ``HISTORY_LIMIT``, oldest first), then matching live events,
    each as ``event: log`` whose data is a rendered ``_event_row.html``.

    Filters: ``level`` minimum level, ``job`` exact job id, ``kind`` kind prefix, ``q``
    case-insensitive message substring. FastAPI adds the ``: ping`` heartbeat after 15 s idle
    and the ``Cache-Control: no-cache`` / ``X-Accel-Buffering: no`` headers.
    """
    job_id, kind_prefix, text = job or None, kind or None, q or None
    needle = text.translate(_ASCII_LOWER) if text else None

    def matches(e: Event) -> bool:
        return (
            (level is None or _LEVEL_ORDER[e.level] >= _LEVEL_ORDER[level])
            and (job_id is None or e.job_id == job_id)
            and (kind_prefix is None or e.kind.value.startswith(kind_prefix))
            and (needle is None or needle in e.message.translate(_ASCII_LOWER))
        )

    # Subscribe before reading history so nothing emitted in between is lost. An event that
    # lands in both is sent once: live events with an id <= the last history id are skipped.
    live = services.events.subscribe()
    try:
        history = await _cancel_safe(
            services.events.query(
                level=level,
                job_id=job_id,
                kind_prefix=kind_prefix,
                text=text,
                limit=HISTORY_LIMIT,
                newest=True,
            )
        )
        last_id = max((e.id for e in history if e.id is not None), default=0)
        for e in history:
            yield _log_event(e)
        async for e in live:
            if e.id is not None and e.id <= last_id:
                continue
            if matches(e):
                yield _log_event(e)
    finally:
        # Client disconnect cancels this generator; never leave a subscriber queue behind.
        await live.aclose()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", {"levels": list(EventLevel)}, headers=_NO_STORE
    )


@router.get("/gui/partials/now", response_class=HTMLResponse)
async def now_partial(
    request: Request, services: Annotated[Services, Depends(get_services)]
) -> HTMLResponse:
    running: list[Job] = []
    for job_id in sorted(services.jobs.running):  # sorted() copies: the runner mutates the set
        row = await services.job_store.row(job_id)
        if row is not None:
            running.append(to_job(row))
    context = {
        "running": running,
        "queue_depth": services.jobs.queue_depth,
        "recent": await services.job_store.recent(RECENT_JOBS),
    }
    return templates.TemplateResponse(request, "_now.html", context, headers=_NO_STORE)


@router.get("/gui/partials/health", response_class=HTMLResponse)
async def health_partial(
    request: Request, services: Annotated[Services, Depends(get_services)]
) -> HTMLResponse:
    deps = await cached_dependencies(services)  # same 5 s cached single-flight run as /health
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    context = {
        "deps": deps,
        "overall": overall_state(deps),
        "hit_rate": services.cache.stats().hit_rate,
        "jobs_today": await services.job_store.count_since(midnight),
    }
    return templates.TemplateResponse(request, "_health.html", context, headers=_NO_STORE)
