import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
import structlog
from research_engine.errors import ServiceError
from research_engine.events.sqlite import SqliteEventBus
from research_engine.jobs import runner as runner_mod
from research_engine.jobs.context import JobContext
from research_engine.jobs.runner import JobRunner
from research_engine.jobs.store import JobStore
from research_engine.store.db import create_engine_for, init_db
from research_engine.store.tables import JobRow
from research_engine_client.models import (
    BatchFetchRequest,
    BatchFetchResult,
    ErrorCode,
    ErrorDetail,
    EventKind,
    JobStatus,
    JobType,
    SearchReadRequest,
    SearchReadResult,
    SearchRequest,
    SearchResponse,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

REQ = BatchFetchRequest(urls=("https://a.example/",))
SR_REQ = SearchReadRequest(search=SearchRequest(query="python asyncio"))

Make = Callable[..., Awaitable[tuple[JobRunner, SqliteEventBus, JobStore]]]


@pytest.fixture
async def make() -> AsyncIterator[Make]:
    """Factory for (runner, bus, store) on a fresh in-memory DB; always stops and disposes."""
    made: list[tuple[JobRunner, AsyncEngine]] = []

    async def _make(
        workers: int = 2, timeout_s: float = 5, cancel_grace_s: float = 0.2
    ) -> tuple[JobRunner, SqliteEventBus, JobStore]:
        engine = create_engine_for(":memory:")
        await init_db(engine)
        bus = SqliteEventBus(engine)
        store = JobStore(engine)
        runner = JobRunner(
            store, bus, workers=workers, timeout_s=timeout_s, cancel_grace_s=cancel_grace_s
        )
        made.append((runner, engine))
        return runner, bus, store

    yield _make
    for runner, engine in made:
        await runner.stop()
        await engine.dispose()


def _other_tasks() -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def ok_handler(ctx: JobContext) -> BatchFetchResult:
    await ctx.progress(1, 1, current="https://a.example/")
    return BatchFetchResult(documents=[], failed=[])


async def partial_handler(ctx: JobContext) -> BatchFetchResult:
    ctx.add_error(ErrorDetail(code=ErrorCode.FETCH_FAILED, message="x", retryable=True, source="u"))
    return BatchFetchResult(documents=[], failed=[])


async def blocking_handler(ctx: JobContext) -> BatchFetchResult:
    await asyncio.sleep(10)
    raise AssertionError("unreachable")


# --- brief tests ---------------------------------------------------------------------------


async def test_done_flow(make: Make) -> None:
    runner, bus, _ = await make()
    runner.register(JobType.FETCH_BATCH, ok_handler)
    await runner.start()
    job = await runner.submit(JobType.FETCH_BATCH, REQ)
    assert job.status is JobStatus.QUEUED and len(job.id) == 32
    int(job.id, 16)
    detail = await runner.wait(job.id, 5)
    assert detail is not None and detail.job.status is JobStatus.DONE
    assert detail.job.progress.done == 1 and isinstance(detail.result, BatchFetchResult)
    assert detail.job.started_at is not None and detail.job.finished_at is not None
    assert detail.job.started_at.tzinfo is UTC and detail.job.created_at.tzinfo is UTC
    assert detail.job.result_ref == f"/v1/jobs/{job.id}"
    kinds = [e.kind for e in await bus.query(job_id=job.id)]
    assert kinds[:2] == [EventKind.JOB_QUEUED, EventKind.JOB_STARTED]
    assert EventKind.JOB_PROGRESS in kinds and kinds[-1] is EventKind.JOB_DONE
    await runner.stop()


async def test_partial_and_failed(make: Make) -> None:
    runner, bus, _ = await make()

    async def failing(ctx: JobContext) -> SearchReadResult:
        raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "searxng down", retryable=True)

    runner.register(JobType.FETCH_BATCH, partial_handler)
    runner.register(JobType.SEARCH_READ, failing)
    await runner.start()
    p = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert p is not None and p.job.status is JobStatus.PARTIAL
    assert p.job.errors[0].code is ErrorCode.FETCH_FAILED
    assert isinstance(p.result, BatchFetchResult) and p.job.finished_at is not None
    f = await runner.wait((await runner.submit(JobType.SEARCH_READ, SR_REQ)).id, 5)
    assert f is not None and f.job.status is JobStatus.FAILED and f.result is None
    assert f.job.errors[0].code is ErrorCode.UPSTREAM_ERROR and f.job.finished_at is not None
    assert (await bus.query(job_id=p.job.id))[-1].kind is EventKind.JOB_PARTIAL
    assert (await bus.query(job_id=f.job.id))[-1].kind is EventKind.JOB_FAILED
    await runner.stop()


async def test_timeout(make: Make) -> None:
    runner, bus, _ = await make(timeout_s=0.2)
    saw_cancel = asyncio.Event()

    async def slow(ctx: JobContext) -> BatchFetchResult:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            saw_cancel.set()
            raise
        raise AssertionError("unreachable")

    runner.register(JobType.FETCH_BATCH, slow)
    await runner.start()
    d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert d is not None and d.job.status is JobStatus.FAILED
    assert d.job.errors[0].code is ErrorCode.JOB_TIMEOUT and d.job.finished_at is not None
    assert saw_cancel.is_set()  # the handler task was really cancelled, not left running
    assert not [t for t in _other_tasks() if t.get_name().startswith("job-handler")]
    assert (await bus.query(job_id=d.job.id))[-1].kind is EventKind.JOB_FAILED
    await runner.stop()


async def test_cancel_running_and_queued(make: Make) -> None:
    runner, bus, _ = await make(workers=1)
    started = asyncio.Event()
    saw_cancel = asyncio.Event()

    async def blocking(ctx: JobContext) -> BatchFetchResult:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            saw_cancel.set()
            raise
        raise AssertionError("unreachable")

    runner.register(JobType.FETCH_BATCH, blocking)
    await runner.start()
    a = await runner.submit(JobType.FETCH_BATCH, REQ)
    b = await runner.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    cb = await runner.cancel(b.id)
    assert cb is not None and cb.status is JobStatus.CANCELLED and cb.finished_at is not None
    assert cb.errors[0].code is ErrorCode.JOB_CANCELLED
    ca = await runner.cancel(a.id)
    # cancel() on a running job returns once the job is terminal
    assert ca is not None and ca.status is JobStatus.CANCELLED
    assert saw_cancel.is_set()
    d = await runner.wait(a.id, 5)
    assert d is not None and d.job.status is JobStatus.CANCELLED
    assert d.job.errors[0].code is ErrorCode.JOB_CANCELLED and d.job.finished_at is not None
    assert (await bus.query(job_id=a.id))[-1].kind is EventKind.JOB_CANCELLED
    assert (await bus.query(job_id=b.id))[-1].kind is EventKind.JOB_CANCELLED
    assert await runner.cancel("nope") is None
    await runner.stop()


async def test_wait_times_out_returns_current(make: Make) -> None:
    runner, _, _ = await make()
    gate = asyncio.Event()

    async def gated(ctx: JobContext) -> BatchFetchResult:
        await gate.wait()
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, gated)
    await runner.start()
    j = await runner.submit(JobType.FETCH_BATCH, REQ)
    d = await runner.wait(j.id, 0.2)
    assert d is not None and d.job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    gate.set()
    d = await runner.wait(j.id, 5)
    assert d is not None and d.job.status is JobStatus.DONE
    await runner.stop()


async def test_concurrency(make: Make) -> None:
    runner, _, _ = await make(workers=2)
    running = 0
    peak = 0

    async def h(ctx: JobContext) -> BatchFetchResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, h)
    await runner.start()
    jobs = [await runner.submit(JobType.FETCH_BATCH, REQ) for _ in range(2)]
    for j in jobs:
        await runner.wait(j.id, 5)
    assert peak == 2
    await runner.stop()


async def test_startup_recovery(make: Make) -> None:
    runner, bus, store = await make()
    runner.register(JobType.FETCH_BATCH, ok_handler)
    j = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)
    await store.force_status(j.id, JobStatus.RUNNING)  # simulate crash mid-run
    q = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)  # left queued
    await runner.start()
    d = await runner.get(j.id)
    assert d is not None and d.job.status is JobStatus.FAILED
    assert d.job.errors[0].code is ErrorCode.INTERRUPTED and d.job.errors[0].retryable
    assert d.job.finished_at is not None
    assert (await bus.query(job_id=j.id))[-1].kind is EventKind.JOB_FAILED
    dq = await runner.wait(q.id, 5)
    assert dq is not None and dq.job.status is JobStatus.DONE
    await runner.stop()


# --- rulings -------------------------------------------------------------------------------


async def test_worker_survives_cancel_of_running_job(make: Make) -> None:
    runner, _, _ = await make(workers=1)
    started = asyncio.Event()

    async def h(ctx: JobContext) -> BatchFetchResult:
        if ctx.request == REQ:
            started.set()
            await asyncio.sleep(10)
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, h)
    await runner.start()
    a = await runner.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    await runner.cancel(a.id)
    other = BatchFetchRequest(urls=("https://b.example/",))
    b = await runner.submit(JobType.FETCH_BATCH, other)
    d = await runner.wait(b.id, 2)
    assert d is not None and d.job.status is JobStatus.DONE  # the single worker is still alive
    await runner.stop()


async def test_stop_interrupts_running_job_and_leaves_no_tasks(make: Make) -> None:
    runner, bus, _ = await make(workers=2)
    started = asyncio.Event()

    async def h(ctx: JobContext) -> BatchFetchResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    runner.register(JobType.FETCH_BATCH, h)
    await runner.start()
    j = await runner.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(runner.stop(), 2)
    d = await runner.get(j.id)
    assert d is not None and d.job.status is JobStatus.FAILED
    assert d.job.errors[-1].code is ErrorCode.INTERRUPTED and d.job.errors[-1].retryable
    assert d.job.finished_at is not None
    assert (await bus.query(job_id=j.id))[-1].kind is EventKind.JOB_FAILED
    assert _other_tasks() == []
    assert runner.running == set()


async def test_stop_leaves_unstarted_jobs_queued_for_next_start(make: Make) -> None:
    runner, _, _ = await make(workers=1)
    started = asyncio.Event()

    async def h(ctx: JobContext) -> BatchFetchResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    runner.register(JobType.FETCH_BATCH, h)
    await runner.start()
    await runner.submit(JobType.FETCH_BATCH, REQ)
    q = await runner.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    await runner.stop()
    d = await runner.get(q.id)
    assert d is not None and d.job.status is JobStatus.QUEUED
    runner.register(JobType.FETCH_BATCH, ok_handler)
    await runner.start()
    d = await runner.wait(q.id, 2)
    assert d is not None and d.job.status is JobStatus.DONE
    await runner.stop()


async def test_cancel_is_bounded_when_handler_swallows_cancellation(make: Make) -> None:
    runner, _, _ = await make(workers=1, cancel_grace_s=0.1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn(ctx: JobContext) -> BatchFetchResult:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()  # swallows cancellation and keeps going
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, stubborn)
    await runner.start()
    a = await runner.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    ca = await asyncio.wait_for(runner.cancel(a.id), 1)
    assert ca is not None and ca.status is JobStatus.CANCELLED
    # the runner is not wedged: the worker moved on
    runner.register(JobType.FETCH_BATCH, ok_handler)
    d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 2)
    assert d is not None and d.job.status is JobStatus.DONE
    release.set()
    await runner.stop()
    # the late return of the abandoned handler did not overwrite the terminal state
    d = await runner.get(a.id)
    assert d is not None and d.job.status is JobStatus.CANCELLED and d.result is None
    assert _other_tasks() == []


async def test_timeout_is_bounded_when_handler_swallows_cancellation(make: Make) -> None:
    runner, _, _ = await make(workers=1, timeout_s=0.1, cancel_grace_s=0.1)
    release = asyncio.Event()

    async def stubborn(ctx: JobContext) -> BatchFetchResult:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, stubborn)
    await runner.start()
    d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 2)
    assert d is not None and d.job.status is JobStatus.FAILED
    assert d.job.errors[-1].code is ErrorCode.JOB_TIMEOUT
    release.set()
    await runner.stop()
    assert _other_tasks() == []


async def test_cancel_terminal_job_returns_unchanged(make: Make) -> None:
    runner, _, _ = await make()
    runner.register(JobType.FETCH_BATCH, ok_handler)
    await runner.start()
    j = await runner.submit(JobType.FETCH_BATCH, REQ)
    done = await runner.wait(j.id, 5)
    assert done is not None and done.job.status is JobStatus.DONE
    again = await runner.cancel(j.id)
    assert again == done.job
    await runner.stop()


async def test_wait_does_not_leak_events(make: Make) -> None:
    runner, _, _ = await make()
    gate = asyncio.Event()

    async def gated(ctx: JobContext) -> BatchFetchResult:
        await gate.wait()
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, gated)
    await runner.start()
    jobs = [await runner.submit(JobType.FETCH_BATCH, REQ) for _ in range(3)]
    assert runner.waiter_count == 0  # submit alone allocates nothing
    await runner.wait(jobs[0].id, 0.05)  # times out
    assert runner.waiter_count == 0
    waits = [asyncio.create_task(runner.wait(j.id, 5)) for j in jobs for _ in range(2)]
    await asyncio.sleep(0.05)
    assert runner.waiter_count == 3  # one slot per job, shared by its waiters
    gate.set()
    details = await asyncio.gather(*waits)
    assert all(d is not None and d.job.status is JobStatus.DONE for d in details)
    assert runner.waiter_count == 0
    await runner.wait(jobs[0].id, 5)  # terminal: returns at once
    assert await runner.wait("nope", 5) is None
    assert runner.waiter_count == 0
    await runner.stop()


async def test_wait_is_capped(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    assert runner_mod.MAX_WAIT_S == 60
    monkeypatch.setattr(runner_mod, "MAX_WAIT_S", 0.05)
    runner, _, _ = await make()
    gate = asyncio.Event()

    async def gated(ctx: JobContext) -> BatchFetchResult:
        await gate.wait()
        return BatchFetchResult(documents=[], failed=[])

    runner.register(JobType.FETCH_BATCH, gated)
    await runner.start()
    j = await runner.submit(JobType.FETCH_BATCH, REQ)
    d = await asyncio.wait_for(runner.wait(j.id, 3600), 1)
    assert d is not None and not d.job.status.is_terminal
    gate.set()
    await runner.stop()


async def test_unexpected_exception_is_generic_and_logged(make: Make) -> None:
    runner, bus, _ = await make()

    async def boom(ctx: JobContext) -> BatchFetchResult:
        raise RuntimeError("secret internal detail")

    runner.register(JobType.FETCH_BATCH, boom)
    await runner.start()
    with structlog.testing.capture_logs() as logs:
        d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert d is not None and d.job.status is JobStatus.FAILED and d.job.finished_at is not None
    err = d.job.errors[-1]
    assert err.code is ErrorCode.INTERNAL_ERROR and err.message == "internal error"
    events = await bus.query(job_id=d.job.id)
    assert events[-1].kind is EventKind.JOB_FAILED
    assert all("secret" not in e.message for e in events)
    crash = [entry for entry in logs if entry.get("exc_info")]
    assert crash and crash[0]["job_id"] == d.job.id
    await runner.stop()


async def test_handler_raising_timeout_error_is_internal_not_job_timeout(make: Make) -> None:
    runner, _, _ = await make()

    async def own_timeout(ctx: JobContext) -> BatchFetchResult:
        raise TimeoutError

    runner.register(JobType.FETCH_BATCH, own_timeout)
    await runner.start()
    d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert d is not None and d.job.errors[-1].code is ErrorCode.INTERNAL_ERROR
    await runner.stop()


async def test_missing_handler_fails_job_and_worker_survives(make: Make) -> None:
    runner, _, _ = await make(workers=1)
    runner.register(JobType.FETCH_BATCH, ok_handler)
    await runner.start()
    d = await runner.wait((await runner.submit(JobType.SEARCH_READ, SR_REQ)).id, 5)
    assert d is not None and d.job.status is JobStatus.FAILED
    assert d.job.errors[-1].code is ErrorCode.INTERNAL_ERROR
    d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert d is not None and d.job.status is JobStatus.DONE
    await runner.stop()


async def test_context_carries_validated_request_and_bound_emitter(make: Make) -> None:
    runner, bus, _ = await make()
    seen: list[JobContext] = []

    async def h(ctx: JobContext) -> SearchReadResult:
        seen.append(ctx)
        await ctx.emitter.info(EventKind.SEARCH_STARTED, "searching")
        return SearchReadResult(search=SearchResponse(query="q"), documents=[])

    runner.register(JobType.SEARCH_READ, h)
    await runner.start()
    j = await runner.submit(JobType.SEARCH_READ, SR_REQ, session_id="s1")
    d = await runner.wait(j.id, 5)
    assert d is not None and d.job.session_id == "s1" and isinstance(d.result, SearchReadResult)
    ctx = seen[0]
    assert (
        ctx.job_id == j.id and ctx.request == SR_REQ and isinstance(ctx.request, SearchReadRequest)
    )
    assert ctx.emitter.job_id == j.id
    assert EventKind.SEARCH_STARTED in [e.kind for e in await bus.query(job_id=j.id)]
    await runner.stop()


# --- store ---------------------------------------------------------------------------------


@pytest.fixture
async def store() -> AsyncIterator[JobStore]:
    engine = create_engine_for(":memory:")
    await init_db(engine)
    yield JobStore(engine)
    await engine.dispose()


async def test_claim_is_atomic(store: JobStore) -> None:
    j = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)
    results = await asyncio.gather(*(store.claim(j.id) for _ in range(5)))
    assert sorted(results) == [False, False, False, False, True]
    row = await store.row(j.id)
    assert row is not None and row.status == "running" and row.started_at is not None
    assert row.started_at.tzinfo is None  # stored naive UTC


async def test_result_round_trip_both_kinds(store: JobStore) -> None:
    bf = BatchFetchResult(documents=[], failed=[])
    sr = SearchReadResult(search=SearchResponse(query="q", suggestions=["s"]), documents=[])
    for type_, req, result in ((JobType.FETCH_BATCH, REQ, bf), (JobType.SEARCH_READ, SR_REQ, sr)):
        j = await store.create(type_, req, session_id=None)
        assert await store.claim(j.id)
        assert await store.finish(j.id, JobStatus.DONE, result=result, errors=[])
        d = await store.detail(j.id)
        assert d is not None and d.result == result and type(d.result) is type(result)
        assert d.job.request == req.model_dump(mode="json")


async def test_finish_only_once_and_progress_only_while_running(store: JobStore) -> None:
    j = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)
    await store.set_progress(j.id, 1, 2, "x")  # queued: ignored
    assert await store.claim(j.id)
    await store.set_progress(j.id, 1, 2, "x")
    d = await store.detail(j.id)
    assert d is not None and (d.job.progress.done, d.job.progress.total) == (1, 2)
    assert d.job.progress.current == "x"
    assert await store.finish(j.id, JobStatus.CANCELLED, result=None, errors=[])
    assert not await store.finish(j.id, JobStatus.DONE, result=None, errors=[])
    await store.set_progress(j.id, 2, 2, "y")  # terminal: ignored
    d = await store.detail(j.id)
    assert d is not None and d.job.status is JobStatus.CANCELLED and d.job.progress.done == 1
    assert d.job.progress.current is None


async def test_recent_and_count_since(store: JobStore) -> None:
    ids = [(await store.create(JobType.FETCH_BATCH, REQ, session_id=None)).id for _ in range(3)]
    assert [j.id for j in await store.recent(2)] == ids[:0:-1]
    now = datetime.now(UTC)
    assert await store.count_since(now - timedelta(minutes=1)) == 3
    assert await store.count_since(now + timedelta(minutes=1)) == 0
    # a non-UTC aware datetime is normalised to UTC before comparing
    plus2 = timezone(timedelta(hours=2))
    assert await store.count_since((now - timedelta(minutes=1)).astimezone(plus2)) == 3
    assert await store.count_since((now + timedelta(minutes=1)).astimezone(plus2)) == 0


async def test_create_stores_naive_utc(store: JobStore) -> None:
    j = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)
    async with AsyncSession(store.engine) as s:
        row = await s.get(JobRow, j.id)
    assert row is not None and row.created_at.tzinfo is None
    assert abs(row.created_at.replace(tzinfo=UTC) - datetime.now(UTC)) < timedelta(seconds=5)
    assert j.created_at.tzinfo is UTC
