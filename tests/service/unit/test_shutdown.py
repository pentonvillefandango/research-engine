"""Early shutdown hook: SIGTERM ends SSE streams and wakes long-polls before uvicorn's graceful
wait, so the shutdown budget is not eaten by open connections (V1-15, V1-16)."""

import asyncio
import os
import signal
import sys
import time
from types import FrameType

import httpx
import pytest
from research_engine.events.base import Emitter
from research_engine.events.memory import InMemoryEventBus
from research_engine.gui.session import COOKIE, SessionCodec
from research_engine.shutdown import begin_shutdown, shutdown_signals
from research_engine_client.models import BatchFetchRequest, EventKind, JobStatus, JobType

from tests.conftest import ROOT, TEST_ENV


async def test_close_subscribers_ends_open_and_new_subscriptions() -> None:
    bus = InMemoryEventBus()
    sub = bus.subscribe()
    got: list[str] = []

    async def consume() -> None:
        async for e in sub:
            got.append(e.message)

    task = asyncio.create_task(consume())
    await Emitter(bus).info(EventKind.JOB_STARTED, "before")
    await asyncio.sleep(0)
    bus.close_subscribers()
    await asyncio.wait_for(task, 1.0)  # async for ended naturally
    assert got == ["before"]
    assert bus.subscriber_count == 0
    late = bus.subscribe()  # after shutdown began: ends at once
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(late), 1.0)
    assert bus.subscriber_count == 0


async def test_begin_shutdown_ends_bus_subscriptions_and_wakes_waiters(app) -> None:
    services = app.state.services
    sub = services.events.subscribe()
    job = await services.job_store.create(
        JobType.FETCH_BATCH, BatchFetchRequest(urls=("https://a.example/",)), session_id=None
    )  # queued, never enqueued: a waiter would sit out its full timeout
    waiter = asyncio.create_task(services.jobs.wait(job.id, 30))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    t0 = time.perf_counter()
    begin_shutdown(app)
    detail = await asyncio.wait_for(waiter, 1.0)
    assert time.perf_counter() - t0 < 1.0
    assert detail is not None and detail.job.status is JobStatus.QUEUED  # current state
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(sub), 1.0)
    assert app.state.shutting_down is True
    # A long-poll that starts after shutdown began returns at once too.
    again = await asyncio.wait_for(services.jobs.wait(job.id, 30), 1.0)
    assert again is not None and again.job.status is JobStatus.QUEUED


async def test_shutdown_signals_chain_and_restore() -> None:
    calls: list[str] = []

    def previous(signum: int, frame: FrameType | None) -> None:
        calls.append(f"previous:{signum}")

    original = signal.signal(signal.SIGTERM, previous)
    try:
        with shutdown_signals(lambda: calls.append("hook")):
            assert signal.getsignal(signal.SIGTERM) is not previous
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(0.01)  # the hook is scheduled with call_soon_threadsafe
        assert calls == [f"previous:{signal.SIGTERM}", "hook"]
        assert signal.getsignal(signal.SIGTERM) is previous  # restored
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_shutdown_signals_noop_off_main_thread() -> None:
    before = signal.getsignal(signal.SIGTERM)

    def in_thread() -> object:
        async def inner() -> object:
            with shutdown_signals(lambda: None):
                return signal.getsignal(signal.SIGTERM)

        return asyncio.run(inner())

    assert await asyncio.to_thread(in_thread) is before


_SERVER = """
import uvicorn
from research_engine.app import create_app
from research_engine.config import Settings
from research_engine.testing import build_test_services

settings = Settings()
app = create_app(settings, services=build_test_services(settings))
uvicorn.run(app, host="127.0.0.1", port=0, log_level="info", timeout_graceful_shutdown=5)
"""


async def test_sigterm_with_open_sse_stream_exits_fast() -> None:
    """Real uvicorn process: without the hook it would sit in its 5 s graceful wait for the
    open stream; with it the stream ends at once, lifespan shutdown runs, the process exits."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "SESSION_"))}
    env |= TEST_ENV | {"LOG_LEVEL": "INFO", "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SERVER,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout_task = asyncio.create_task(proc.stdout.read())
    stderr_lines: list[str] = []
    try:
        async with asyncio.timeout(20):
            while True:
                line = (await proc.stderr.readline()).decode()
                assert line, "".join(stderr_lines)  # EOF: the server died during startup
                stderr_lines.append(line)
                if "Uvicorn running on http://127.0.0.1:" in line:
                    port = int(line.split("127.0.0.1:")[1].split()[0])
                    break
        stderr_task = asyncio.create_task(proc.stderr.read())
        cookie = SessionCodec(TEST_ENV["SESSION_SECRET"]).issue()
        async with (
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Cookie": f"{COOKIE}={cookie}"},
                timeout=10,
            ) as c,
            c.stream("GET", "/gui/events/stream") as resp,
        ):
            assert resp.status_code == 200
            chunks = resp.aiter_text()
            first = await asyncio.wait_for(anext(chunks), 5)  # history: system.startup
            assert "event: log" in first
            t0 = time.perf_counter()
            proc.send_signal(signal.SIGTERM)
            async with asyncio.timeout(2):
                async for _ in chunks:  # the stream must end, not hang
                    pass
            await asyncio.wait_for(proc.wait(), 2)
            elapsed = time.perf_counter() - t0
        print(f"\nSIGTERM to process exit with an open SSE stream: {elapsed * 1000:.0f} ms")
        assert elapsed < 2.0
        err = "".join(stderr_lines) + (await stderr_task).decode()
        out = (await stdout_task).decode()
        # uvicorn logs this only after our lifespan shutdown (runner stop etc.) has returned.
        assert "Waiting for application shutdown." in err, err
        assert "Application shutdown complete." in err, err
        assert "shutdown begun" in out  # the hook ran
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
