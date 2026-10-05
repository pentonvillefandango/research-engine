"""GUI routes (login, logout, dashboard, live event stream, partials) and the GUI
security-headers middleware (B2, V1-15, V1-16)."""

import asyncio
import json
import string
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import structlog
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
from research_engine.api.jobs import valid_job_id
from research_engine.gui.jsonview import to_tree
from research_engine.gui.render import LINK_REL, render_untrusted_markdown
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
_log = structlog.get_logger("research_engine.gui")

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


HISTORY_QUERY_TIMEOUT_S = 10.0
"""Upper bound for one stream's history query; past it the stream goes on with live events."""


class _StreamSlots:
    def __init__(self) -> None:
        self.active = 0  # slots taken: open streams plus abandoned history queries
        self.detached = 0  # of those, abandoned (client gone) history queries still running


class _Slot:
    """One stream's reservation. Freed when the stream ends, unless its shielded history query
    is still running (the client disconnected mid-query): then only when that query finishes,
    so open/close cycles cannot pile up queries beyond ``MAX_EVENT_STREAMS``."""

    def __init__(self, slots: _StreamSlots) -> None:
        self._slots = slots
        self._query: asyncio.Task[Any] | None = None
        self._freed = False
        slots.active += 1

    def hold_for(self, task: asyncio.Task[Any]) -> None:
        self._query = task

    def release(self) -> None:
        query = self._query
        if query is None or query.done():
            self._free()
            return
        self._slots.detached += 1

        def _done(_t: asyncio.Task[Any]) -> None:
            self._slots.detached -= 1
            self._free()

        query.add_done_callback(_done)

    def _free(self) -> None:
        if not self._freed:
            self._freed = True
            self._slots.active -= 1


def _stream_slots(services: Services) -> _StreamSlots:
    return services.extra.setdefault(_STREAMS_KEY, _StreamSlots())


def active_streams(services: Services) -> int:
    """Taken stream slots: open ``/gui/events/stream`` connections plus abandoned history
    queries still running."""
    return _stream_slots(services).active


def detached_queries(services: Services) -> int:
    """History queries still running after their client disconnected."""
    return _stream_slots(services).detached


async def _stream_slot(
    services: Annotated[Services, Depends(get_services)],
) -> AsyncIterator[_Slot]:
    """Reserve one stream slot for the whole response, or 503.

    A dependency with ``yield``: FastAPI runs its exit code after the streaming response has
    finished. Check and increment happen with no ``await`` in between, so concurrent requests
    cannot overshoot the cap.
    """
    slots = _stream_slots(services)
    if slots.active >= MAX_EVENT_STREAMS:
        raise HTTPException(503, "too many open event streams", headers={"Retry-After": "10"})
    slot = _Slot(slots)
    try:
        yield slot
    finally:
        slot.release()


_detached: set[asyncio.Task[Any]] = set()


def _forget(task: asyncio.Task[Any]) -> None:
    _detached.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved: no "exception was never retrieved" noise after a disconnect


async def _with_timeout[T](coro: Coroutine[Any, Any, T]) -> T:
    async with asyncio.timeout(HISTORY_QUERY_TIMEOUT_S):
        return await coro


async def _cancel_safe[T](coro: Coroutine[Any, Any, T], slot: _Slot) -> T:
    """Await database work in its own task, shielded from the caller's cancellation and
    bounded by ``HISTORY_QUERY_TIMEOUT_S`` (applied inside the task).

    On client disconnect FastAPI cancels the SSE generator through an anyio cancel scope. That
    cancellation is level-triggered: after a mid-query cancel it also cancels SQLAlchemy's
    cleanup awaits (invalidate / close), so the pooled connection is never checked back in and
    the pool slot is lost. A separate task is outside that scope: the query finishes (or times
    out, cancelled once, cleanly) and returns its connection; only this await is abandoned.
    ``slot`` stays taken until the task is done.
    """
    task = asyncio.ensure_future(_with_timeout(coro))
    _detached.add(task)
    task.add_done_callback(_forget)
    slot.hold_for(task)
    return await asyncio.shield(task)


def _fmt_time(ts: datetime) -> str:
    ts = ts.astimezone(UTC)
    return f"{ts:%H:%M:%S}.{ts.microsecond // 1000:03d}"


def _event_context(e: Event) -> dict[str, Any]:
    data_json = json.dumps(e.data, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    return {"e": e, "time": _fmt_time(e.ts), "data_json": data_json}


def render_event_row(e: Event) -> str:
    """One ``_event_row.html`` fragment. Jinja autoescapes every field; ``data`` is shown as
    escaped, pretty-printed JSON. Newlines are kept: FastAPI splits a multi-line ``raw_data``
    into one ``data:`` line each, which ``EventSource`` joins back with ``\n``."""
    return templates.env.get_template("_event_row.html").render(**_event_context(e)).strip()


def _log_event(e: Event) -> ServerSentEvent:
    # ``id`` equals the row's ``data-id``: the client de-duplicates on it without parsing HTML.
    return ServerSentEvent(
        event="log", id=None if e.id is None else str(e.id), raw_data=render_event_row(e)
    )


@router.get(
    "/gui/events/stream",
    response_class=EventSourceResponse,
)
async def event_stream(
    services: Annotated[Services, Depends(get_services)],
    slot: Annotated[_Slot, Depends(_stream_slot)],
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
        try:
            history = await _cancel_safe(
                services.events.query(
                    level=level,
                    job_id=job_id,
                    kind_prefix=kind_prefix,
                    text=text,
                    limit=HISTORY_LIMIT,
                    newest=True,
                ),
                slot,
            )
        except TimeoutError:
            _log.warning("event stream history query timed out", timeout_s=HISTORY_QUERY_TIMEOUT_S)
            history = []  # go on with live events only
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


# --- job detail ----------------------------------------------------------------------------------

TIMELINE_LIMIT = 500
"""Oldest job events shown on the detail page (the live stream carries on beyond them)."""
MAX_MARKDOWN_CHARS = 200_000
MAX_LINKS = 50
MAX_TABLES = 20
MAX_TABLE_ROWS = 200


def is_http_url(value: object) -> bool:
    """True for an absolute http(s) URL with no whitespace or control characters; only those
    are ever rendered as links."""
    if not isinstance(value, str) or not value or any(c <= " " or c == "\x7f" for c in value):
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _clip_markdown(text: str) -> tuple[str, bool]:
    return (text[:MAX_MARKDOWN_CHARS], True) if len(text) > MAX_MARKDOWN_CHARS else (text, False)


templates.env.globals.update(
    json_tree=to_tree,
    is_http_url=is_http_url,
    link_rel=LINK_REL,
    untrusted_markdown=render_untrusted_markdown,
    clip_markdown=_clip_markdown,
    max_markdown_chars=MAX_MARKDOWN_CHARS,
    max_links=MAX_LINKS,
    max_tables=MAX_TABLES,
    max_table_rows=MAX_TABLE_ROWS,
)


def _job_not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "job_not_found.html", {}, status_code=404, headers=_NO_STORE
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request, job_id: str, services: Annotated[Services, Depends(get_services)]
) -> HTMLResponse:
    detail = await services.jobs.get(job_id) if valid_job_id(job_id) else None
    if detail is None:
        return _job_not_found(request)
    job = detail.job
    events = await services.events.query(job_id=job_id, newest=False, limit=TIMELINE_LIMIT + 1)
    context = {
        "job": job,
        "terminal": job.status.is_terminal,
        "result": detail.result,
        "rows": [_event_context(e) for e in events[:TIMELINE_LIMIT]],
        "more_events": len(events) > TIMELINE_LIMIT,
        "timeline_limit": TIMELINE_LIMIT,
    }
    return templates.TemplateResponse(request, "job.html", context, headers=_NO_STORE)


@router.get("/gui/partials/job/{job_id}/status", response_class=HTMLResponse)
async def job_status_partial(
    request: Request,
    job_id: str,
    services: Annotated[Services, Depends(get_services)],
    live: bool = False,
) -> HTMLResponse:
    detail = await services.jobs.get(job_id) if valid_job_id(job_id) else None
    if detail is None:
        return HTMLResponse(
            '<section id="job-status" class="panel"><p class="error">Job not found.</p></section>',
            status_code=404,
            headers=_NO_STORE,
        )
    job = detail.job
    headers = dict(_NO_STORE)
    if live and job.status.is_terminal:
        # A poll that has just seen the job finish: reload so the results appear.
        headers["HX-Refresh"] = "true"
    return templates.TemplateResponse(
        request,
        "_job_status.html",
        {"job": job, "terminal": job.status.is_terminal},
        headers=headers,
    )
