"""In-process asyncio job runner backed by the SQLite queue (V1-09).

Task layout: each worker owns at most one *handler task* at a time. Only the worker ever
cancels its handler task; ``cancel()``, the job timeout and ``stop()`` just record a reason
and wake the worker, which cancels the handler, waits a bounded grace period for it to
unwind, and then writes the terminal state. A handler that swallows ``CancelledError`` is
abandoned after the grace period (kept referenced, re-cancelled at ``stop()``), so it can
never wedge a worker. ``JobStore.finish``/``set_progress`` only touch ``running`` rows, so an
abandoned handler that later returns cannot overwrite the terminal state.
"""

import asyncio
import contextlib
import enum
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel
from research_engine_client.models import (
    BatchFetchRequest,
    BatchFetchResult,
    ErrorCode,
    ErrorDetail,
    EventKind,
    Job,
    JobDetail,
    JobStatus,
    JobType,
    SearchReadRequest,
    SearchReadResult,
)

from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink

from .context import JobContext, JobHandler
from .store import JobStore

_log = structlog.get_logger("research_engine.jobs")
_REQUEST_MODELS: dict[JobType, type[BaseModel]] = {
    JobType.FETCH_BATCH: BatchFetchRequest,
    JobType.SEARCH_READ: SearchReadRequest,
}
_RESULT_MODELS: dict[JobType, type[BaseModel]] = {
    JobType.FETCH_BATCH: BatchFetchResult,
    JobType.SEARCH_READ: SearchReadResult,
}
MAX_WAIT_S = 60.0
CANCEL_WAIT_S = 5.0
"""Upper bound for ``cancel()`` waiting on a running job to reach a terminal state."""


class _Stop(enum.Enum):
    CANCEL = "cancel"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"


@dataclass
class _Active:
    """A job a worker has dequeued (registered before the claim, so cancel() never misses it)."""

    job_id: str
    reason: _Stop | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def interrupt(self, reason: _Stop) -> None:
        if self.reason is None:
            self.reason = reason
        self.wake.set()


@dataclass
class _WaitSlot:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    refs: int = 0


@dataclass
class _Outcome:
    status: JobStatus
    kind: EventKind
    message: str
    result: BaseModel | None = None


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        events: EventSink,
        *,
        workers: int,
        timeout_s: float,
        cancel_grace_s: float = 2.0,
    ) -> None:
        self._store, self._events = store, events
        self._workers_n, self._timeout, self._grace = workers, timeout_s, cancel_grace_s
        self._handlers: dict[JobType, JobHandler] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._waiters: dict[str, _WaitSlot] = {}
        self._active: dict[str, _Active] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._idle: set[asyncio.Task[Any]] = set()
        self._abandoned: set[asyncio.Task[BaseModel]] = set()
        self._stopping = False
        self.running: set[str] = set()
        """Ids of jobs whose handler is currently executing (GUI "Now" panel)."""

    def register(self, type_: JobType, handler: JobHandler) -> None:
        self._handlers[type_] = handler

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def waiter_count(self) -> int:
        """Live ``wait()`` slots (one per job with at least one waiter)."""
        return len(self._waiters)

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        """Fail jobs a previous process left ``running``; re-enqueue ``queued``; start workers."""
        if self._workers:
            return
        for job_id in await self._store.ids_with_status(JobStatus.RUNNING):
            err = ErrorDetail(
                code=ErrorCode.INTERRUPTED,
                message="service restarted while job was running",
                retryable=True,
            )
            if await self._store.finish(job_id, JobStatus.FAILED, result=None, errors=[err]):
                await Emitter(self._events, job_id).error(
                    EventKind.JOB_FAILED, "job interrupted by restart"
                )
        self._stopping = False
        self._queue = asyncio.Queue()  # the DB is the source of truth; drop stale ids
        for job_id in await self._store.ids_with_status(JobStatus.QUEUED):
            self._queue.put_nowait(job_id)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"job-worker-{i}")
            for i in range(self._workers_n)
        ]

    async def stop(self) -> None:
        """Stop the workers. A job mid-run ends ``failed``/``interrupted``; queued ones stay."""
        if not self._workers:
            return
        self._stopping = True
        for active in list(self._active.values()):
            active.interrupt(_Stop.SHUTDOWN)
        for worker in self._idle:
            worker.cancel()  # parked on queue.get(): safe to cancel
        # Busy workers finalise their job (bounded by the grace period) and then exit.
        _, pending = await asyncio.wait(self._workers, timeout=self._grace + CANCEL_WAIT_S)
        for worker in pending:  # last resort; should not happen
            _log.warning("job worker did not stop in time", worker=worker.get_name())
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._idle.clear()
        await self._reap_abandoned()

    async def _reap_abandoned(self) -> None:
        if not self._abandoned:
            return
        for task in self._abandoned:
            task.cancel()
        _, pending = await asyncio.wait(self._abandoned, timeout=self._grace)
        for task in pending:
            _log.error(
                "job handler ignored cancellation and is still running", task=task.get_name()
            )

    # --- public API ----------------------------------------------------------------------

    async def submit(
        self, type_: JobType, request: BaseModel, *, session_id: str | None = None
    ) -> Job:
        job = await self._store.create(type_, request, session_id=session_id)
        try:
            await Emitter(self._events, job.id).info(
                EventKind.JOB_QUEUED, f"{type_.value} queued", type=type_.value
            )
        finally:
            self._queue.put_nowait(job.id)  # the row exists: never strand it in 'queued'
        return job

    async def get(self, job_id: str) -> JobDetail | None:
        return await self._store.detail(job_id)

    async def wait(self, job_id: str, timeout_s: float) -> JobDetail | None:
        """Long-poll: return as soon as the job is terminal, or after ``timeout_s`` (max 60 s)."""
        # Register before reading the row, so a finish between the read and the wait is seen.
        slot = self._waiters.setdefault(job_id, _WaitSlot())
        slot.refs += 1
        try:
            detail = await self._store.detail(job_id)
            if detail is None or detail.job.status.is_terminal:
                return detail
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(slot.event.wait(), max(0.0, min(timeout_s, MAX_WAIT_S)))
            return await self._store.detail(job_id)
        finally:
            slot.refs -= 1
            if slot.refs == 0 and self._waiters.get(job_id) is slot:
                del self._waiters[job_id]

    async def cancel(self, job_id: str) -> Job | None:
        """Cancel a queued or running job; a terminal job is returned unchanged, unknown -> None."""
        if await self._store.cancel_if_queued(job_id):
            await Emitter(self._events, job_id).info(
                EventKind.JOB_CANCELLED, "cancelled before start"
            )
            self._signal(job_id)
        elif (active := self._active.get(job_id)) is not None:
            active.interrupt(_Stop.CANCEL)
            try:
                await asyncio.wait_for(active.finished.wait(), CANCEL_WAIT_S)
            except TimeoutError:
                _log.warning("cancel: job did not reach a terminal state in time", job_id=job_id)
        detail = await self._store.detail(job_id)
        return detail.job if detail else None

    def _signal(self, job_id: str) -> None:
        if (slot := self._waiters.pop(job_id, None)) is not None:
            slot.event.set()

    # --- workers -------------------------------------------------------------------------

    async def _worker(self) -> None:
        me = asyncio.current_task()
        if me is None:  # pragma: no cover - always run as a task
            raise RuntimeError("job worker must run in a task")
        while not self._stopping:
            self._idle.add(me)
            try:
                job_id = await self._queue.get()
            finally:
                self._idle.discard(me)
            if self._stopping:
                return  # leave it queued in the DB; the next start() re-enqueues it
            if job_id in self._active:
                continue  # duplicate queue entry: another worker already owns this job
            active = self._active[job_id] = _Active(job_id)
            try:
                if await self._store.claim(job_id):
                    await self._run(active)
            except Exception:
                _log.exception("job worker error", job_id=job_id)
                with contextlib.suppress(Exception):  # best effort: don't leave it 'running'
                    errors: list[ErrorDetail] = []
                    outcome = _internal_error(errors)
                    if await self._store.finish(
                        job_id, JobStatus.FAILED, result=None, errors=errors
                    ):
                        await Emitter(self._events, job_id).error(outcome.kind, outcome.message)
            finally:
                self._active.pop(job_id, None)
                active.finished.set()
                self._signal(job_id)

    async def _run(self, active: _Active) -> None:
        job_id = active.job_id
        em = Emitter(self._events, job_id)
        row = await self._store.row(job_id)
        if row is None:  # claimed a moment ago; only an external delete gets here
            raise RuntimeError(f"job row {job_id} vanished")
        type_ = JobType(row.type)
        await em.info(EventKind.JOB_STARTED, f"{type_.value} started")
        errors: list[ErrorDetail] = []
        handler = self._handlers.get(type_)
        request = self._parse_request(job_id, type_, row.request_json)
        if handler is None:
            _log.error("no handler registered for job type", job_id=job_id, type=type_.value)
        if handler is None or request is None:
            outcome = _internal_error(errors)
        elif active.reason is not None:  # cancelled or stopped between claim and start
            outcome = self._interrupted(active.reason, errors)
        else:

            async def persist(done: int, total: int, current: str | None) -> None:
                await self._store.set_progress(job_id, done, total, current)

            ctx = JobContext(
                job_id=job_id, request=request, emitter=em, _persist_progress=persist, errors=errors
            )
            outcome = await self._execute(active, type_, handler, ctx)
        if await self._store.finish(job_id, outcome.status, result=outcome.result, errors=errors):
            log = em.error if outcome.status is JobStatus.FAILED else em.info
            await log(outcome.kind, outcome.message, errors=len(errors))

    @staticmethod
    def _parse_request(job_id: str, type_: JobType, request_json: str) -> BaseModel | None:
        try:
            return _REQUEST_MODELS[type_].model_validate_json(request_json)
        except Exception:
            _log.exception("stored job request is invalid", job_id=job_id)
            return None

    async def _execute(
        self, active: _Active, type_: JobType, handler: JobHandler, ctx: JobContext
    ) -> _Outcome:
        """Run the handler under timeout/cancel/stop and map what happened to an outcome."""
        job_id = ctx.job_id

        async def call() -> BaseModel:
            return await handler(ctx)

        task = asyncio.create_task(call(), name=f"job-handler-{job_id}")
        self.running.add(job_id)
        interrupted = False  # True once this worker cancelled the handler
        try:
            wake = asyncio.create_task(active.wake.wait(), name=f"job-wake-{job_id}")
            try:
                await asyncio.wait(
                    {task, wake}, timeout=self._timeout, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not task.done():
                active.interrupt(_Stop.TIMEOUT)  # keeps an earlier cancel/shutdown reason
                interrupted = True
                task.cancel()
                await asyncio.wait({task}, timeout=self._grace)
                if not task.done():
                    _log.error("job handler ignored cancellation; abandoning it", job_id=job_id)
                    self._abandon(task)
        except asyncio.CancelledError:
            # The worker itself was cancelled (stop() last resort): never orphan the handler.
            task.cancel()
            self._abandon(task)
            raise
        finally:
            self.running.discard(job_id)

        errors = ctx.errors
        if interrupted and active.reason is not None:
            # Whatever the handler did after being cancelled (re-raise, swallow and return,
            # or keep running), the job ends as the interruption reason says. A handler that
            # had already finished on its own keeps its own outcome.
            return self._interrupted(active.reason, errors)
        if task.cancelled():  # cancelled by something other than the runner
            return self._interrupted(_Stop.CANCEL, errors)
        exc = task.exception()
        if isinstance(exc, ServiceError):
            errors.append(exc.detail)
            return _Outcome(
                JobStatus.FAILED, EventKind.JOB_FAILED, f"job failed: {exc.detail.message}"
            )
        if exc is not None:
            _log.error("job handler crashed", job_id=job_id, exc_info=exc)
            return _internal_error(errors)
        result = task.result()
        if not isinstance(result, _RESULT_MODELS[type_]):
            _log.error(
                "job handler returned the wrong result type",
                job_id=job_id,
                got=type(result).__name__,
            )
            return _internal_error(errors)
        status = JobStatus.PARTIAL if errors else JobStatus.DONE
        kind = EventKind.JOB_PARTIAL if errors else EventKind.JOB_DONE
        return _Outcome(status, kind, f"job {status.value} ({len(errors)} errors)", result)

    def _abandon(self, task: asyncio.Task[BaseModel]) -> None:
        self._abandoned.add(task)

        def _done(t: asyncio.Task[BaseModel]) -> None:
            self._abandoned.discard(t)
            if not t.cancelled():
                t.exception()  # mark retrieved; the job is already terminal

        task.add_done_callback(_done)

    def _interrupted(self, reason: _Stop, errors: list[ErrorDetail]) -> _Outcome:
        if reason is _Stop.CANCEL:
            errors.append(
                ErrorDetail(code=ErrorCode.JOB_CANCELLED, message="cancelled", retryable=False)
            )
            return _Outcome(JobStatus.CANCELLED, EventKind.JOB_CANCELLED, "job cancelled")
        if reason is _Stop.TIMEOUT:
            errors.append(
                ErrorDetail(
                    code=ErrorCode.JOB_TIMEOUT,
                    message=f"exceeded {self._timeout:g}s",
                    retryable=True,
                )
            )
            return _Outcome(JobStatus.FAILED, EventKind.JOB_FAILED, "job timed out")
        errors.append(
            ErrorDetail(
                code=ErrorCode.INTERRUPTED,
                message="service stopped while job was running",
                retryable=True,
            )
        )
        return _Outcome(JobStatus.FAILED, EventKind.JOB_FAILED, "job interrupted by shutdown")


def _internal_error(errors: list[ErrorDetail]) -> _Outcome:
    """Unexpected failure: the generic text only, never the exception message."""
    errors.append(
        ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="internal error", retryable=False)
    )
    return _Outcome(JobStatus.FAILED, EventKind.JOB_FAILED, "job failed: internal error")
