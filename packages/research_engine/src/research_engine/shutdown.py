"""Early shutdown hook: react to SIGTERM/SIGINT *before* uvicorn's graceful wait.

On a signal, uvicorn (0.54) closes its listeners, marks in-flight responses ``keep_alive=False``
and then waits up to ``--timeout-graceful-shutdown`` for open connections, cancels what is
left, and only then runs the lifespan shutdown. Nothing tells the app that shutdown has begun,
so never-ending responses (the GUI's SSE event stream) and ``?wait=`` long-polls would use the
whole graceful timeout, which adds to the lifespan's own shutdown time.

``shutdown_signals`` chains a handler in front of uvicorn's (uvicorn installs its handlers with
``signal.signal`` before lifespan startup, so ``signal.getsignal`` returns them) that schedules
``begin_shutdown`` on the event loop and then calls the previous handler. ``begin_shutdown``
ends every event subscription (each SSE stream then finishes normally) and wakes every job
long-poll, so the open connections drain at once and uvicorn moves straight on to lifespan
shutdown.
"""

import asyncio
import contextlib
import os
import signal
import threading
from collections.abc import Callable, Iterator
from types import FrameType

import structlog
from fastapi import FastAPI

_log = structlog.get_logger("research_engine.shutdown")
SIGNALS = (signal.SIGTERM, signal.SIGINT)


def begin_shutdown(app: FastAPI) -> None:
    """Shutdown has begun: end SSE streams and long-polls now. Idempotent, synchronous."""
    if getattr(app.state, "shutting_down", False):
        return
    app.state.shutting_down = True
    services = app.state.services
    services.events.close_subscribers()
    services.jobs.wake_all_waiters()
    _log.info("shutdown begun: closed event streams and woke long-polls")


@contextlib.contextmanager
def shutdown_signals(on_shutdown: Callable[[], None]) -> Iterator[None]:
    """While active, SIGTERM and SIGINT first schedule ``on_shutdown`` on the running loop, then
    run the previously installed handler. Restores the previous handlers on exit.

    Only in the main thread (Python only delivers signals there); elsewhere a no-op. Must be
    entered with a running event loop.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    previous: dict[int, signal.Handlers | Callable[[int, FrameType | None], object] | int | None]
    previous = {}

    def handler(signum: int, frame: FrameType | None) -> None:
        # Signal handlers run between bytecodes on the main thread: hand over thread-safely.
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(on_shutdown)
        prev = previous.get(signum)
        if callable(prev):
            prev(signum, frame)
        elif prev == signal.SIG_DFL:  # nobody else handles it: keep the default behaviour
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        # SIG_IGN or None (installed outside Python): nothing more to do

    for sig in SIGNALS:
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, prev in previous.items():
            if prev is not None:
                signal.signal(sig, prev)
