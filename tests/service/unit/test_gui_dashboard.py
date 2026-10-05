"""Live activity dashboard: SSE event stream, Now panel, health strip (V1-15, V1-16)."""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import fastapi.routing
import fastapi.sse
import httpx
import pytest
from research_engine.events.base import Emitter
from research_engine.gui import routes as gui_routes
from research_engine.gui.session import COOKIE
from research_engine.jobs.runner import JobRunner
from research_engine.store.tables import EventRow
from research_engine_client.models import (
    BatchFetchRequest,
    Event,
    EventKind,
    EventLevel,
    JobType,
)
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

GUI = Path(__file__).resolve().parents[3] / "packages/research_engine/src/research_engine/gui"


# --- helpers ---------------------------------------------------------------------------------


@asynccontextmanager
async def logged_in(app) -> AsyncIterator[httpx.AsyncClient]:
    # A context manager rather than the brief's ``await logged_in(app)``: httpx 0.28 refuses
    # ``async with`` on a client that has already sent a request.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://research.localhost") as c:
        r = await c.post(
            "/login",
            data={"api_key": "test-key", "next": "/"},
            headers={"Origin": "http://research.localhost"},
        )
        assert r.status_code == 303
        yield c


def live_client(app, base_url: str) -> httpx.AsyncClient:
    """A client for the real uvicorn ``live_server`` carrying a valid GUI session cookie."""
    cookie = app.state.session_codec.issue()
    return httpx.AsyncClient(base_url=base_url, headers={"Cookie": f"{COOKIE}={cookie}"}, timeout=5)


async def _until(cond: Callable[[], bool], within_s: float = 3.0) -> None:
    # Polling is deliberate: subscriber and slot counts expose no change notification.
    async with asyncio.timeout(within_s):
        while not cond():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def _subscribed(app, before: int) -> None:
    """Wait until the stream's generator has registered its live subscription."""
    bus = app.state.services.events
    await _until(lambda: bus.subscriber_count > before)


@dataclass
class Frame:
    event: str | None = None
    data: list[str] = field(default_factory=list[str])
    id: str | None = None
    comments: list[str] = field(default_factory=list[str])

    @property
    def text(self) -> str:
        return "\n".join(self.data)


def parse_sse(buf: str) -> list[Frame]:
    """Parse complete SSE blocks per the WHATWG rules; every non-comment line must be a known
    field (so injected text can only ever appear inside a ``data`` value)."""
    frames: list[Frame] = []
    complete = buf[: buf.rfind("\n\n") + 2] if "\n\n" in buf else ""
    for block in complete.split("\n\n"):
        if not block:
            continue
        f = Frame()
        for line in block.split("\n"):
            if line.startswith(":"):
                f.comments.append(line[1:].strip())
                continue
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            assert name in {"event", "data", "id", "retry"}, line
            if name == "event":
                f.event = value
            elif name == "data":
                f.data.append(value)
            elif name == "id":
                f.id = value
        frames.append(f)
    return frames


async def _read_until(resp: httpx.Response, needle: str, buf: list[str] | None = None) -> str:
    acc = buf if buf is not None else []
    async for chunk in resp.aiter_text():
        acc.append(chunk)
        joined = "".join(acc)
        # Only count the needle once the frame holding it is complete.
        if needle in joined[: joined.rfind("\n\n")]:
            return joined
    raise AssertionError("stream ended")


def log_texts(buf: str) -> list[str]:
    return [f.text for f in parse_sse(buf) if f.event == "log"]


# --- brief tests (run against a real uvicorn: ASGITransport buffers streaming bodies) ---------


async def test_live_event_within_one_second(app, live_server: str) -> None:
    bus = app.state.services.events
    before = bus.subscriber_count
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", "/gui/events/stream?kind=job.") as resp,
    ):
        assert resp.headers["content-type"].startswith("text/event-stream")
        await _subscribed(app, before)
        await Emitter(bus, "jobX").info(EventKind.FETCH_STARTED, "filtered-out")
        t0 = time.perf_counter()
        await Emitter(bus, "jobX").info(EventKind.JOB_STARTED, "live-<b>marker</b>")
        buf = await asyncio.wait_for(_read_until(resp, "live-"), 1.0)
        latency = time.perf_counter() - t0
    print(f"\nSSE event latency: {latency * 1000:.1f} ms")
    assert latency < 1.0
    assert "event: log" in buf and "filtered-out" not in buf
    assert "&lt;b&gt;marker&lt;/b&gt;" in buf  # escaped, never raw HTML
    assert "<b>" not in buf


async def test_history_first(app, live_server: str) -> None:
    bus = app.state.services.events
    await Emitter(bus).warning(EventKind.SEARCH_ENGINE_FAILED, "history-row")
    await Emitter(bus).info(EventKind.SEARCH_DONE, "below-level")
    before = bus.subscriber_count
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", "/gui/events/stream?level=warning") as resp,
    ):
        await _subscribed(app, before)
        await Emitter(bus).error(EventKind.FETCH_FAILED, "live-row")
        buf = await asyncio.wait_for(_read_until(resp, "live-row"), 1.0)
    texts = log_texts(buf)
    hist = next(i for i, t in enumerate(texts) if "history-row" in t)
    live = next(i for i, t in enumerate(texts) if "live-row" in t)
    assert hist < live
    assert "below-level" not in buf


async def test_dashboard_and_partials(app) -> None:
    async with logged_in(app) as c:
        page = await c.get("/")
        assert page.status_code == 200 and 'sse-connect="/gui/events/stream"' in page.text
        assert (await c.get("/gui/partials/now")).status_code == 200
        health = await c.get("/gui/partials/health")
        assert "searxng" in health.text and "jobs today" in health.text.lower()


# --- stream: headers, heartbeat, filters, history ---------------------------------------------


async def test_stream_headers_and_heartbeat(
    app,
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FastAPI's EventSourceResponse sends ": ping" after 15 s without an event.
    assert fastapi.sse._PING_INTERVAL == 15.0
    assert fastapi.routing._PING_INTERVAL == 15.0  # pyright: ignore[reportPrivateImportUsage]
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.2)
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", "/gui/events/stream?kind=robots.") as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-accel-buffering"] == "no"
        assert resp.headers["cache-control"] == "no-cache"
        assert "content-security-policy" in resp.headers
        buf = await asyncio.wait_for(_read_until(resp, ": ping"), 2.0)
    assert any("ping" in f.comments for f in parse_sse(buf))


_FILTER_EVENTS = [  # (level, job, kind, message)
    (EventLevel.INFO, None, EventKind.JOB_STARTED, "e0 plain"),
    (EventLevel.WARNING, None, EventKind.SEARCH_ENGINE_FAILED, "e1 warn"),
    (EventLevel.ERROR, "j1", EventKind.FETCH_FAILED, "e2 err needle"),
    (EventLevel.DEBUG, "j1", EventKind.FETCH_STARTED, "e3 dbg"),
    (EventLevel.INFO, "j2", EventKind.FETCH_DONE, "e4 has NEEDLE"),
]


async def _emit_set(bus, phase: str) -> None:
    for level, job, kind, msg in _FILTER_EVENTS:
        await getattr(Emitter(bus, job), level.value)(kind, f"{phase}-{msg}")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("level=warning", {"e1", "e2"}),
        ("job=j1", {"e2", "e3"}),
        ("kind=fetch.", {"e2", "e3", "e4"}),
        ("q=nEeDlE", {"e2", "e4"}),
        ("level=info&kind=fetch.&q=needle", {"e2", "e4"}),
        ("level=&job=&kind=&q=", {"e0", "e1", "e2", "e3", "e4"}),  # empty form fields = no filter
    ],
)
async def test_filters_apply_to_history_and_live(
    app,
    live_server: str,
    query: str,
    expected: set[str],
) -> None:
    bus = app.state.services.events
    await _emit_set(bus, "H")
    before = bus.subscriber_count
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", f"/gui/events/stream?{query}") as resp,
    ):
        assert resp.status_code == 200
        await _subscribed(app, before)
        await _emit_set(bus, "L")
        # Matches every filter above, so it always arrives last.
        await Emitter(bus, "j1").error(EventKind.FETCH_FAILED, "END needle")
        buf = await asyncio.wait_for(_read_until(resp, "END needle"), 2.0)
    texts = log_texts(buf)
    for phase in ("H", "L"):
        got = {m.group(1) for t in texts if (m := re.search(rf"{phase}-(e\d)", t))}
        assert got == expected, (phase, query, texts)


async def test_history_is_newest_200_matching_from_the_store(
    app,
    live_server: str,
) -> None:
    """History comes from a filtered store query, not from the last N events overall: 1000
    newer non-matching events must not push the matching ones out."""
    services = app.state.services
    bus = services.events
    for i in range(210):
        await Emitter(bus).info(EventKind.JOB_PROGRESS, f"h{i:03d}")
    async with AsyncSession(services.engine) as s:
        now = datetime.now(UTC).replace(tzinfo=None)
        s.add_all(
            EventRow(
                ts=now,
                job_id=None,
                level="info",
                kind="cache.miss",
                message="noise",
                data_json="{}",
            )
            for _ in range(1000)
        )
        await s.commit()
    before = bus.subscriber_count
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", "/gui/events/stream?kind=job.progress") as resp,
    ):
        await _subscribed(app, before)
        await Emitter(bus).info(EventKind.JOB_PROGRESS, "sentinel")
        buf = await asyncio.wait_for(_read_until(resp, "sentinel"), 3.0)
    texts = log_texts(buf)
    history = [m.group(0) for t in texts if (m := re.search(r"h\d{3}", t))]
    assert len(history) == 200
    assert history[0] == "h010" and history[-1] == "h209"
    assert history == sorted(history)
    assert "noise" not in buf


async def test_event_in_subscribe_gap_is_sent_once(
    app,
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscribe-before-history means an event emitted while history is read is both in the
    history and in the live queue; the server drops the live copy (id <= last history id)."""
    bus = app.state.services.events
    real_query = bus.query

    async def racing_query(**kw):
        await Emitter(bus).info(EventKind.JOB_STARTED, "gap-event")
        return await real_query(**kw)

    monkeypatch.setattr(bus, "query", racing_query)
    before = bus.subscriber_count
    async with live_client(app, live_server) as c, c.stream("GET", "/gui/events/stream") as resp:
        await _subscribed(app, before)
        await Emitter(bus).info(EventKind.JOB_DONE, "after-gap")
        buf = await asyncio.wait_for(_read_until(resp, "after-gap"), 2.0)
    texts = log_texts(buf)
    assert sum("gap-event" in t for t in texts) == 1
    ids = [f.id for f in parse_sse(buf) if f.event == "log"]
    assert len(ids) == len(set(ids))


async def test_sse_framing_cannot_be_broken(app, live_server: str) -> None:
    bus = app.state.services.events
    before = bus.subscriber_count
    nasty = "start\n\ndata: injected\r\nevent: evil\rid: 999\n<b>bold</b> end-marker"
    async with live_client(app, live_server) as c, c.stream("GET", "/gui/events/stream") as resp:
        await _subscribed(app, before)
        await Emitter(bus, "j<x>").info(
            EventKind.JOB_STARTED, nasty, payload="<script>alert(1)</script>\n\ndata: x"
        )
        buf = await asyncio.wait_for(_read_until(resp, "end-marker"), 2.0)
    frames = parse_sse(buf)  # asserts every line is a known field
    hits = [f for f in frames if "end-marker" in f.text]
    assert len(hits) == 1
    frame = hits[0]
    assert frame.event == "log"
    assert frame.id is not None and frame.id != "999"
    assert all(f.event in (None, "log") for f in frames)
    html = frame.text
    assert html.startswith('<div class="ev ') and html.rstrip().endswith("</div>")
    assert f'data-id="{frame.id}"' in html  # the SSE id is the client's de-dupe key
    assert "<b>" not in html and "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "j&lt;x&gt;" in html and "<x>" not in html


# --- stream: validation, auth, lifecycle -----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "level=bogus",
        "level=WARN",
        "kind=Job.",
        "kind=job-x",
        "kind=" + "a" * 65,
        "job=" + "x" * 65,
        "q=" + "x" * 201,
    ],
)
async def test_stream_rejects_bad_filters(app, query: str) -> None:
    async with logged_in(app) as c:
        r = await c.get(f"/gui/events/stream?{query}")
    assert r.status_code == 422


async def test_stream_requires_login(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://research.localhost") as c:
        r = await c.get("/gui/events/stream")
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
        for path in ("/gui/partials/now", "/gui/partials/health", "/"):
            assert (await c.get(path)).status_code == 303


async def test_disconnect_closes_subscription(app, live_server: str) -> None:
    bus = app.state.services.events
    before = bus.subscriber_count
    async with live_client(app, live_server) as c:
        async with c.stream("GET", "/gui/events/stream") as resp:
            assert resp.status_code == 200
            await _subscribed(app, before)
            assert bus.subscriber_count == before + 1
            assert gui_routes.active_streams(app.state.services) == 1
        # Client gone: the generator's finally must aclose() the subscription and free the slot.
        await _until(lambda: bus.subscriber_count == before)
        await _until(lambda: gui_routes.active_streams(app.state.services) == 0)


_SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) "
    "SELECT count(*) FROM c"
)


async def test_disconnect_during_history_query_returns_db_connection(
    app,
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect cancels the stream through an anyio cancel scope, which is level-triggered:
    SQLAlchemy's cleanup after a mid-query cancel is cancelled too, so the pooled connection
    would never be checked in. The history query must be shielded from that."""
    services = app.state.services
    bus = services.events
    real_query = bus.query
    started = asyncio.Event()

    async def slow_query(**kw):
        async with AsyncSession(services.engine) as s:
            started.set()
            await s.exec(text(_SLOW_SQL))  # pyright: ignore[reportCallIssue, reportArgumentType]
        return await real_query(**kw)

    monkeypatch.setattr(bus, "query", slow_query)
    before = bus.subscriber_count
    async with live_client(app, live_server) as c:
        async with c.stream("GET", "/gui/events/stream") as resp:
            assert resp.status_code == 200
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.sleep(0.05)  # the slow query is now running
        await _until(lambda: bus.subscriber_count == before)
    # The single :memory: pool slot must be back (a leak shows as a 2 s pool TimeoutError).
    assert isinstance(await asyncio.wait_for(services.job_store.recent(1), 10), list)


async def test_concurrent_stream_cap(
    app,
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gui_routes.MAX_EVENT_STREAMS == 50
    monkeypatch.setattr(gui_routes, "MAX_EVENT_STREAMS", 2)
    bus = app.state.services.events
    before = bus.subscriber_count
    async with live_client(app, live_server) as c:
        async with (
            c.stream("GET", "/gui/events/stream") as r1,
            c.stream("GET", "/gui/events/stream") as r2,
        ):
            assert r1.status_code == r2.status_code == 200
            await _until(lambda: bus.subscriber_count == before + 2)
            r3 = await c.get("/gui/events/stream")
            assert r3.status_code == 503
            assert r3.headers.get("retry-after")
            assert bus.subscriber_count == before + 2
        await _until(lambda: gui_routes.active_streams(app.state.services) == 0)
        async with c.stream("GET", "/gui/events/stream") as r4:
            assert r4.status_code == 200


# --- event row ---------------------------------------------------------------------------------


def test_event_row_rendering_escapes_everything() -> None:
    e = Event(
        id=7,
        ts=datetime(2026, 10, 5, 13, 4, 5, 678901, tzinfo=UTC),
        job_id="j<1>",
        level=EventLevel.WARNING,
        kind=EventKind.FETCH_FAILED,
        message='<img src=x onerror="alert(1)">',
        data={"html": "<script>x</script>", "n": 1},
    )
    html = gui_routes.render_event_row(e)
    assert html.startswith('<div class="ev ev-warning" data-id="7">')
    assert "13:04:05.678" in html
    assert "warning" in html and "fetch.failed" in html
    assert "<img" not in html and "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in html
    assert 'href="/jobs/j%3C1%3E"' in html and ">j&lt;1&gt;</a>" in html
    assert "<details>" in html
    assert "<script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert "\n  &#34;n&#34;: 1" in html  # pretty-printed JSON, escaped


def test_event_row_without_job_or_data() -> None:
    e = Event(
        id=8,
        ts=datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC),
        level=EventLevel.INFO,
        kind=EventKind.SYSTEM_STARTUP,
        message="service started",
    )
    html = gui_routes.render_event_row(e)
    assert "00:00:00.000" in html
    assert "<a " not in html and "<details" not in html


# --- dashboard page and partials ---------------------------------------------------------------


async def test_dashboard_page_wiring(app) -> None:
    async with logged_in(app) as c:
        html = (await c.get("/")).text
    log = re.search(r'<div id="log"[^>]*>', html)
    assert log, html
    tag = log.group(0)
    for attr in (
        'hx-ext="sse"',
        'sse-connect="/gui/events/stream"',
        'sse-swap="log"',
        'hx-swap="beforeend"',
    ):
        assert attr in tag, attr
    assert re.search(
        r'hx-get="/gui/partials/now"[^>]*'
        r'hx-trigger="load, every 2s, htmx:sseMessage from:#log throttle:500ms"',
        html,
    )
    assert re.search(r'hx-get="/gui/partials/health"[^>]*hx-trigger="load, every 10s"', html)
    form = re.search(r'<form id="filters".*?</form>', html, re.S)
    assert form
    for name in ("level", "job", "kind", "q"):
        assert f'name="{name}"' in form.group(0), name
    assert 'id="pause"' in html
    assert "hx-on" not in html and " style=" not in html


async def test_now_partial(app, monkeypatch: pytest.MonkeyPatch) -> None:
    services = app.state.services
    store = services.job_store
    req = BatchFetchRequest(urls=("https://a.example/",))
    jobs = [await store.create(JobType.FETCH_BATCH, req, session_id=None) for _ in range(12)]
    running = jobs[0]
    assert await store.claim(running.id)
    long_url = "https://evil.example/<script>alert(1)</script>/" + "a" * 200
    await store.set_progress(running.id, 2, 5, long_url)
    monkeypatch.setattr(services.jobs, "running", {running.id})
    monkeypatch.setattr(JobRunner, "queue_depth", property(lambda self: 7))
    async with logged_in(app) as c:
        r = await c.get("/gui/partials/now")
    html = r.text
    assert r.status_code == 200
    running_section = re.search(r'<table class="running".*?</table>', html, re.S)
    assert running_section, html
    rs = running_section.group(0)
    assert f'href="/jobs/{running.id}"' in rs and "fetch_batch" in rs and "2/5" in rs
    assert "<script>" not in html and "&lt;script&gt;" in rs
    assert "a" * 200 not in rs  # truncated
    assert re.search(r'class="queue-depth"[^>]*>7<', html)
    recent = re.search(r'<table class="recent".*?</table>', html, re.S)
    assert recent
    links = re.findall(r'href="/jobs/([^"]+)"', recent.group(0))
    assert links == [j.id for j in reversed(jobs)][:10]


async def test_now_partial_empty(app) -> None:
    async with logged_in(app) as c:
        r = await c.get("/gui/partials/now")
    assert r.status_code == 200 and "No running jobs" in r.text


async def test_health_partial(
    app, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = app.state.services
    calls = {"searxng": 0}

    async def searxng_down() -> bool:
        calls["searxng"] += 1
        return False

    monkeypatch.setitem(services.health_checks, "searxng", searxng_down)
    req = BatchFetchRequest(urls=("https://a.example/",))
    for _ in range(3):
        await services.job_store.create(JobType.FETCH_BATCH, req, session_id=None)
    await services.cache.get("missing-key")  # one miss -> hit rate 0 %
    assert (await client.get("/health")).status_code == 200
    async with logged_in(app) as c:
        r = await c.get("/gui/partials/health")
    html = r.text
    assert r.status_code == 200
    assert calls["searxng"] == 1  # shares /health's 5 s cached single-flight check
    assert re.search(r'class="pill pill-down"[^>]*>\s*searxng', html), html
    assert re.search(r'class="pill pill-up"[^>]*>\s*crawl4ai', html)
    assert re.search(r'class="pill pill-up"[^>]*>\s*database', html)
    assert re.search(r'class="pill pill-(up|degraded)"[^>]*>\s*cache', html)
    assert re.search(r"Cache hit rate[^<]*<[^>]*>0%", html)
    assert re.search(r"Jobs today[^<]*<[^>]*>3<", html)


# --- app.js --------------------------------------------------------------------------------


def test_app_js_is_csp_safe_and_complete() -> None:
    js = (GUI / "static/app.js").read_text()
    for banned in (
        "eval(",
        "new Function",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        'setTimeout("',
        'setInterval("',
    ):
        assert banned not in js, banned
    assert not re.search(r"\.on[a-z]+\s*=", js), "use addEventListener, not on* properties"
    assert "addEventListener" in js
    for needed in (
        "URLSearchParams",
        "htmx.process",
        "htmx.swap",
        "htmx:sseBeforeMessage",
        "createEventSource",
        ".close()",
    ):
        assert needed in js, needed
    assert re.search(r"MAX_ROWS\s*=\s*2000", js)
