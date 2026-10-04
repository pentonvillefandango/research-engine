# Step 4: SQLite store, job runner, cache and event bus

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** V1-09 (jobs), V1-10 (cache), V1-15 (event log), D9 (SQLite WAL).

**Outcome:**
- Persistent SQLite storage in WAL mode, using SQLModel and aiosqlite.
- `SqliteEventBus` (persisted, queryable, live fan-out) and `SqliteCache` replace the in-memory seams behind the same protocols.
- A `JobStore` plus `JobRunner` with asyncio workers handle submit, progress, wait, cancel, timeout and interrupted-job recovery.
- `GET /v1/jobs/{id}?wait=` and `DELETE /v1/jobs/{id}`.

**Task order:**
- 4.1 runs first.
- 4.2 and 4.3 run in parallel; they touch disjoint files.
- 4.4 runs after both.
- 4.5 runs last.

---

### Task 4.1: Database module

**Goal:** Create the async SQLite engine with WAL and busy-timeout pragmas, the SQLModel table definitions for jobs, events and cache, and `init_db()`.

**Files:**
- Modify: `packages/research_engine/pyproject.toml` (deps: `sqlmodel`, `aiosqlite`, `greenlet`)
- Create: `packages/research_engine/src/research_engine/store/{__init__,db,tables}.py`
- Test: `tests/service/unit/test_db.py`

**Acceptance Criteria:**
- [ ] `create_engine_for(path)` returns an `AsyncEngine`.
  - For a file path, it creates the parent directory and sets `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL` and `foreign_keys=ON` on every connection.
  - For `":memory:"`, it uses `StaticPool`, so all sessions share one database.
- [ ] `init_db(engine)` creates the tables `job`, `event` and `cache_entry`, with these indexes:
  - `event(ts)`, `event(job_id)`, `event(kind)`;
  - `job(status, created_at)`;
  - `cache_entry(expires_at)`.
- [ ] A test on a `tmp_path` file confirms `PRAGMA journal_mode` returns `wal`.
- [ ] `init_db` is idempotent: calling it twice doesn't fail.

**Verify:** `uv run pytest tests/service/unit/test_db.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_db.py
from pathlib import Path

from sqlalchemy import text

from research_engine.store.db import create_engine_for, init_db


async def test_wal_and_tables(tmp_path: Path) -> None:
    engine = create_engine_for(str(tmp_path / "sub" / "re.sqlite"))
    await init_db(engine)
    await init_db(engine)
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
        names = {r[0] for r in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        indexes = {r[0] for r in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}
    assert mode == "wal"
    assert {"job", "event", "cache_entry"} <= names
    assert {"ix_event_ts", "ix_event_job_id", "ix_event_kind", "ix_job_status_created",
            "ix_cache_entry_expires_at"} <= indexes
    await engine.dispose()


async def test_memory_shared_between_sessions() -> None:
    engine = create_engine_for(":memory:")
    await init_db(engine)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO cache_entry(key, value, expires_at) VALUES ('k', x'00', 1)"))
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cache_entry"))).scalar_one() == 1
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Add the dependencies.** `uv add --package research-engine "sqlmodel>=0.0.47" "aiosqlite>=0.22.1" "greenlet>=3.5.6"`.

- [ ] **Step 4: Implement `store/tables.py`**

```python
"""SQLite tables (D9). JSON payloads are stored as text and validated through the public models on read."""

from datetime import datetime

from sqlalchemy import Column, Index, LargeBinary
from sqlmodel import Field, SQLModel


class JobRow(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (Index("ix_job_status_created", "status", "created_at"),)
    id: str = Field(primary_key=True)
    type: str
    status: str
    progress_done: int = 0
    progress_total: int = 0
    progress_current: str | None = None
    parent_id: str | None = None
    session_id: str | None = Field(default=None, index=True)
    request_json: str
    result_json: str | None = None
    errors_json: str = "[]"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EventRow(SQLModel, table=True):
    __tablename__ = "event"
    __table_args__ = (Index("ix_event_ts", "ts"), Index("ix_event_job_id", "job_id"), Index("ix_event_kind", "kind"))
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime
    job_id: str | None = None
    level: str
    kind: str
    message: str
    data_json: str = "{}"


class CacheRow(SQLModel, table=True):
    __tablename__ = "cache_entry"
    key: str = Field(primary_key=True)
    value: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    expires_at: float = Field(index=True)
```

SQLite drops timezone info from datetimes. Store UTC, and re-attach `UTC` when converting rows back to models; Tasks 4.2 and 4.4 do this in `_to_model`.

- [ ] **Step 5: Implement `store/db.py`**

```python
"""Async SQLite engine with WAL (D9, V1-09/10/15)."""

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from . import tables  # noqa: F401 - registers tables on SQLModel.metadata


def create_engine_for(path: str) -> AsyncEngine:
    if path == ":memory:":
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                     connect_args={"check_same_thread": False})
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        if path != ":memory:":
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
```

- [ ] **Step 6: Run the tests.** Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(store): SQLite WAL engine and tables (D9, V1-09, V1-10, V1-15)"`

---

### Task 4.2: SqliteEventBus: persist, query, fan out, retention

**Goal:** Persist every event, assign its sequence id, fan it out live through `InMemoryEventBus`, support filtered history queries for the GUI, and prune old events.

**Files:**
- Create: `packages/research_engine/src/research_engine/events/sqlite.py`
- Test: `tests/service/unit/test_events_sqlite.py`

**Acceptance Criteria:**
- [ ] `emit(event)` inserts a row and then publishes the event to live subscribers, with its store-assigned `id`, so subscribers always see `id` set.
- [ ] `query(level=, job_id=, kind_prefix=, text=, after_id=, limit=100)` returns events in ascending id order with these filters:
  - `level` means that level or higher (debug < info < warning < error);
  - `kind_prefix` matches `search.` and similar prefixes;
  - `text` is a case-insensitive substring of the message;
  - `limit` is capped at 1000.
- [ ] `prune(older_than_days)` deletes events older than the cutoff and returns the count.
- [ ] Concurrent emits from 50 tasks all persist with unique, increasing ids.

**Verify:** `uv run pytest tests/service/unit/test_events_sqlite.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_events_sqlite.py
import asyncio
from datetime import UTC, datetime, timedelta

from research_engine.events.base import Emitter
from research_engine.events.sqlite import SqliteEventBus
from research_engine.store.db import create_engine_for, init_db
from research_engine_client.models import Event, EventKind, EventLevel


async def _bus() -> SqliteEventBus:
    engine = create_engine_for(":memory:")
    await init_db(engine)
    return SqliteEventBus(engine)


async def test_emit_persists_and_fans_out_with_id() -> None:
    bus = await _bus()
    got: list[Event] = []

    async def consume() -> None:
        async for e in bus.subscribe():
            got.append(e)
            return

    t = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await Emitter(bus, "j1").info(EventKind.JOB_STARTED, "go")
    await asyncio.wait_for(t, 1)
    assert got[0].id is not None and got[0].job_id == "j1"
    assert (await bus.query())[0].id == got[0].id


async def test_query_filters() -> None:
    bus = await _bus()
    em = Emitter(bus)
    await em.debug(EventKind.FETCH_STARTED, "fetch a")
    await em.warning(EventKind.SEARCH_ENGINE_FAILED, "Bing failed")
    await em.bind("j2").error(EventKind.FETCH_FAILED, "fetch b broke")
    assert [e.message for e in await bus.query(level=EventLevel.WARNING)] == ["Bing failed", "fetch b broke"]
    assert [e.message for e in await bus.query(kind_prefix="fetch.")] == ["fetch a", "fetch b broke"]
    assert [e.message for e in await bus.query(job_id="j2")] == ["fetch b broke"]
    assert [e.message for e in await bus.query(text="BING")] == ["Bing failed"]
    first = (await bus.query())[0].id
    assert len(await bus.query(after_id=first)) == 2


async def test_prune() -> None:
    bus = await _bus()
    old = Event(ts=datetime.now(UTC) - timedelta(days=40), level=EventLevel.INFO, kind=EventKind.SYSTEM_HEALTH,
                message="old")
    await bus.emit(old)
    await Emitter(bus).info(EventKind.SYSTEM_HEALTH, "new")
    assert await bus.prune(older_than_days=30) == 1
    assert [e.message for e in await bus.query()] == ["new"]


async def test_concurrent_emits_unique_ids() -> None:
    bus = await _bus()
    em = Emitter(bus)
    await asyncio.gather(*(em.info(EventKind.SYSTEM_HEALTH, f"m{i}") for i in range(50)))
    ids = [e.id for e in await bus.query(limit=100)]
    assert len(ids) == 50 and ids == sorted(set(ids))
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `events/sqlite.py`**

```python
"""Persistent event log with live fan-out (V1-15)."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import EventRow
from research_engine_client.models import Event, EventKind, EventLevel

from .memory import InMemoryEventBus

_ORDER = [EventLevel.DEBUG, EventLevel.INFO, EventLevel.WARNING, EventLevel.ERROR]


def _to_model(row: EventRow) -> Event:
    return Event(id=row.id, ts=row.ts.replace(tzinfo=UTC), job_id=row.job_id, level=EventLevel(row.level),
                 kind=EventKind(row.kind), message=row.message, data=json.loads(row.data_json))


class SqliteEventBus:
    def __init__(self, engine: AsyncEngine, live: InMemoryEventBus | None = None) -> None:
        self._engine = engine
        self._live = live or InMemoryEventBus()

    @property
    def subscriber_count(self) -> int:
        return self._live.subscriber_count

    async def emit(self, event: Event) -> None:
        row = EventRow(ts=event.ts.astimezone(UTC).replace(tzinfo=None), job_id=event.job_id,
                       level=event.level.value, kind=event.kind.value, message=event.message,
                       data_json=json.dumps(event.data, default=str))
        async with AsyncSession(self._engine, expire_on_commit=False) as s:
            s.add(row)
            await s.commit()
        await self._live.emit(event.model_copy(update={"id": row.id}))

    def subscribe(self) -> AsyncIterator[Event]:
        return self._live.subscribe()

    async def query(self, *, level: EventLevel | None = None, job_id: str | None = None,
                    kind_prefix: str | None = None, text: str | None = None, after_id: int | None = None,
                    limit: int = 100) -> list[Event]:
        stmt = select(EventRow)
        if level is not None:
            stmt = stmt.where(col(EventRow.level).in_([lv.value for lv in _ORDER[_ORDER.index(level):]]))
        if job_id is not None:
            stmt = stmt.where(EventRow.job_id == job_id)
        if kind_prefix:
            stmt = stmt.where(col(EventRow.kind).startswith(kind_prefix))
        if text:
            stmt = stmt.where(col(EventRow.message).ilike(f"%{text}%"))
        if after_id is not None:
            stmt = stmt.where(col(EventRow.id) > after_id)
        stmt = stmt.order_by(col(EventRow.id)).limit(min(limit, 1000))
        async with AsyncSession(self._engine) as s:
            return [_to_model(r) for r in (await s.exec(stmt)).all()]

    async def prune(self, older_than_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).replace(tzinfo=None)
        async with AsyncSession(self._engine) as s:
            result = await s.exec(delete(EventRow).where(col(EventRow.ts) < cutoff))  # type: ignore[call-overload]
            await s.commit()
            return result.rowcount or 0
```

Note: `query` with no filters returns the **oldest** events first, up to `limit`. The GUI's "recent history" needs the newest 200, so add a `tail(n)` method: select `ORDER BY id DESC LIMIT n`, then reverse. Add a test for it: `tail(2)` after 3 emits returns the last two, in ascending order.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(events): persistent SQLite event bus with query, tail and retention (V1-15)"`

---

### Task 4.3: SqliteCache

**Goal:** A persistent TTL cache behind the `Cache` protocol, with hit and miss stats and pruning of expired entries.

**Files:**
- Create: `packages/research_engine/src/research_engine/cache/sqlite.py`
- Test: `tests/service/unit/test_cache_sqlite.py`

**Acceptance Criteria:**
- [ ] `set` upserts (`INSERT ... ON CONFLICT(key) DO UPDATE`). `get` returns the value before `expires_at` and `None` after it, with an injected wall clock.
- [ ] `stats()` counts hits and misses for the lifetime of the process.
- [ ] `prune()` deletes expired rows and returns the count. `size()` returns the number of rows and the total bytes, for the health strip.
- [ ] Values survive a new `SqliteCache` instance on the same engine.

**Verify:** `uv run pytest tests/service/unit/test_cache_sqlite.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_cache_sqlite.py
from research_engine.cache.sqlite import SqliteCache
from research_engine.store.db import create_engine_for, init_db


class Clock:
    t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


async def test_ttl_upsert_persist_prune() -> None:
    engine = create_engine_for(":memory:")
    await init_db(engine)
    clock = Clock()
    c = SqliteCache(engine, clock=clock)
    await c.set("k", b"v1", ttl_s=10)
    await c.set("k", b"v2", ttl_s=10)
    assert await SqliteCache(engine, clock=clock).get("k") == b"v2"
    clock.t += 11
    assert await c.get("k") is None
    assert (c.stats().hits, c.stats().misses) == (0, 1)
    assert await c.prune() == 1
    assert await c.size() == (0, 0)
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement `cache/sqlite.py`**

```python
"""Persistent TTL cache in SQLite (V1-10)."""

import time
from collections.abc import Callable

from sqlalchemy import delete, func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import CacheRow

from .base import CacheStats


class SqliteCache:
    def __init__(self, engine: AsyncEngine, clock: Callable[[], float] = time.time) -> None:
        self._engine = engine
        self._clock = clock
        self._stats = CacheStats()

    async def get(self, key: str) -> bytes | None:
        async with AsyncSession(self._engine) as s:
            row = (await s.exec(select(CacheRow).where(CacheRow.key == key))).first()
        if row is None or row.expires_at <= self._clock():
            self._stats.misses += 1
            return None
        self._stats.hits += 1
        return row.value

    async def set(self, key: str, value: bytes, ttl_s: int) -> None:
        stmt = insert(CacheRow).values(key=key, value=value, expires_at=self._clock() + ttl_s)
        stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                          set_={"value": stmt.excluded.value, "expires_at": stmt.excluded.expires_at})
        async with AsyncSession(self._engine) as s:
            await s.exec(stmt)  # type: ignore[call-overload]
            await s.commit()

    def stats(self) -> CacheStats:
        return self._stats

    async def prune(self) -> int:
        async with AsyncSession(self._engine) as s:
            res = await s.exec(delete(CacheRow).where(col(CacheRow.expires_at) <= self._clock()))  # type: ignore[call-overload]
            await s.commit()
            return res.rowcount or 0

    async def size(self) -> tuple[int, int]:
        async with AsyncSession(self._engine) as s:
            n, total = (await s.exec(select(func.count(), func.coalesce(func.sum(func.length(CacheRow.value)), 0)))).one()
        return int(n), int(total)
```

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(cache): persistent SQLite TTL cache (V1-10)"`

---

### Task 4.4: JobStore and JobRunner

**Goal:** Build durable jobs: submit returns at once, asyncio workers run registered handlers with progress, error collection, timeout and cancellation, and the store recovers interrupted jobs at startup.

**Files:**
- Create: `packages/research_engine/src/research_engine/jobs/{__init__,store,runner,context}.py`
- Test: `tests/service/unit/test_jobs.py`

**Acceptance Criteria:**
- [ ] `runner.submit(JobType.SEARCH_READ, request_model, session_id=None)` returns a `Job` with `status=queued` and a 32-hex id, persists it, and emits `job.queued`.
- [ ] Workers (`job_workers`) claim queued jobs atomically, using `UPDATE ... WHERE status='queued'` with a rowcount check. They set `running` and `started_at`, and emit `job.started`.
- [ ] Handlers receive a `JobContext` with:
  - `job_id`;
  - `request` (the validated request model);
  - `emitter` (bound to the job);
  - `await ctx.progress(done, total, current=None)`, which persists and emits `job.progress`;
  - `ctx.add_error(ErrorDetail)`;
  - `ctx.errors`.
- [ ] **Outcomes:**

  | What happens | Status | Event |
  |---|---|---|
  | Handler returns, no errors | `done` | `job.done` |
  | Handler returns, with `ctx.errors` | `partial`, result stored | `job.partial` |
  | Handler raises `ServiceError` | `failed`, detail appended | `job.failed` |
  | Handler exceeds `job_timeout_s` | `failed`, code `job_timeout` | `job.failed` |
  | Unexpected exception | `failed`, code `internal_error`, traceback logged | `job.failed` |

  `finished_at` is set in every case.
- [ ] **Cancel:**
  - `cancel(id)` on a queued job sets `cancelled` at once.
  - On a running job, it cancels the task. The handler sees `CancelledError`, and the job ends `cancelled` with error `job_cancelled` and the event `job.cancelled`.
  - On a terminal job, it returns the job unchanged.
  - On an unknown id, it returns `None`.
- [ ] `wait(id, timeout_s)` returns at once if the job is terminal. Otherwise it waits on an in-memory `asyncio.Event` until the job finishes or the timeout passes, and returns the current `JobDetail` either way. `timeout_s` is capped at 60.
- [ ] `get(id)` returns `JobDetail` with `result` parsed as the discriminated union.
- [ ] `start()` does three things:
  1. marks `running` rows as `failed` with `interrupted` (event `job.failed`);
  2. re-enqueues `queued` rows;
  3. starts the workers.

  `stop()` cancels the workers and waits for them.
- [ ] A test proves two jobs run concurrently when `job_workers=2`.

**Verify:** `uv run pytest tests/service/unit/test_jobs.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_jobs.py
import asyncio

import pytest

from research_engine.errors import ServiceError
from research_engine.events.sqlite import SqliteEventBus
from research_engine.jobs.context import JobContext
from research_engine.jobs.runner import JobRunner
from research_engine.jobs.store import JobStore
from research_engine.store.db import create_engine_for, init_db
from research_engine.store.tables import JobRow
from research_engine_client.models import (
    BatchFetchRequest, BatchFetchResult, ErrorCode, ErrorDetail, EventKind, JobStatus, JobType,
)

REQ = BatchFetchRequest(urls=("https://a.example/",))


async def make(workers: int = 2, timeout_s: int = 5) -> tuple[JobRunner, SqliteEventBus, JobStore]:
    engine = create_engine_for(":memory:")
    await init_db(engine)
    bus = SqliteEventBus(engine)
    store = JobStore(engine)
    return JobRunner(store, bus, workers=workers, timeout_s=timeout_s), bus, store


async def ok_handler(ctx: JobContext) -> BatchFetchResult:
    await ctx.progress(1, 1, current="https://a.example/")
    return BatchFetchResult(documents=[], failed=[])


async def partial_handler(ctx: JobContext) -> BatchFetchResult:
    ctx.add_error(ErrorDetail(code=ErrorCode.FETCH_FAILED, message="x", retryable=True, source="u"))
    return BatchFetchResult(documents=[], failed=[])


async def test_done_flow() -> None:
    runner, bus, _ = await make()
    runner.register(JobType.FETCH_BATCH, ok_handler)
    await runner.start()
    job = await runner.submit(JobType.FETCH_BATCH, REQ)
    assert job.status is JobStatus.QUEUED and len(job.id) == 32
    detail = await runner.wait(job.id, 5)
    assert detail is not None and detail.job.status is JobStatus.DONE
    assert detail.job.progress.done == 1 and isinstance(detail.result, BatchFetchResult)
    kinds = [e.kind for e in await bus.query(job_id=job.id)]
    assert kinds[:2] == [EventKind.JOB_QUEUED, EventKind.JOB_STARTED] and kinds[-1] is EventKind.JOB_DONE
    await runner.stop()


async def test_partial_and_failed() -> None:
    runner, _, _ = await make()

    async def failing(ctx: JobContext) -> BatchFetchResult:
        raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "searxng down", retryable=True)

    runner.register(JobType.FETCH_BATCH, partial_handler)
    runner.register(JobType.SEARCH_READ, failing)
    await runner.start()
    p = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert p is not None and p.job.status is JobStatus.PARTIAL and p.job.errors[0].code is ErrorCode.FETCH_FAILED
    f = await runner.wait((await runner.submit(JobType.SEARCH_READ, REQ)).id, 5)
    assert f is not None and f.job.status is JobStatus.FAILED and f.result is None
    await runner.stop()


async def test_timeout() -> None:
    runner, _, _ = await make(timeout_s=1)

    async def slow(ctx: JobContext) -> BatchFetchResult:
        await asyncio.sleep(10)
        raise AssertionError

    runner.register(JobType.FETCH_BATCH, slow)
    await runner.start()
    d = await runner.wait((await runner.submit(JobType.FETCH_BATCH, REQ)).id, 5)
    assert d is not None and d.job.status is JobStatus.FAILED and d.job.errors[0].code is ErrorCode.JOB_TIMEOUT
    await runner.stop()


async def test_cancel_running_and_queued() -> None:
    runner, _, _ = await make(workers=1)
    started = asyncio.Event()

    async def blocking(ctx: JobContext) -> BatchFetchResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError

    runner.register(JobType.FETCH_BATCH, blocking)
    await runner.start()
    a = await runner.submit(JobType.FETCH_BATCH, REQ)
    b = await runner.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    cb = await runner.cancel(b.id)
    assert cb is not None and cb.status is JobStatus.CANCELLED
    await runner.cancel(a.id)
    d = await runner.wait(a.id, 5)
    assert d is not None and d.job.status is JobStatus.CANCELLED
    assert d.job.errors[0].code is ErrorCode.JOB_CANCELLED
    assert await runner.cancel("nope") is None
    await runner.stop()


async def test_wait_times_out_returns_current() -> None:
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
    await runner.stop()


async def test_concurrency() -> None:
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


async def test_startup_recovery() -> None:
    runner, _, store = await make()
    runner.register(JobType.FETCH_BATCH, ok_handler)
    j = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)
    await store.force_status(j.id, JobStatus.RUNNING)          # simulate crash mid-run
    q = await store.create(JobType.FETCH_BATCH, REQ, session_id=None)  # left queued
    await runner.start()
    d = await runner.get(j.id)
    assert d is not None and d.job.status is JobStatus.FAILED and d.job.errors[0].code is ErrorCode.INTERRUPTED
    dq = await runner.wait(q.id, 5)
    assert dq is not None and dq.job.status is JobStatus.DONE
    await runner.stop()


@pytest.mark.parametrize("unused", [JobRow])
def test_row_import(unused: object) -> None:
    assert unused
```

Drop the last test if ruff flags it; it only guards the import path.

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `jobs/context.py`**

```python
"""What a job handler sees."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

from research_engine.events.base import Emitter
from research_engine_client.models import ErrorDetail, EventKind


@dataclass
class JobContext:
    job_id: str
    request: BaseModel
    emitter: Emitter
    _persist_progress: Callable[[int, int, str | None], Awaitable[None]]
    errors: list[ErrorDetail] = field(default_factory=list)

    async def progress(self, done: int, total: int, current: str | None = None) -> None:
        await self._persist_progress(done, total, current)
        await self.emitter.debug(EventKind.JOB_PROGRESS, f"{done}/{total}", done=done, total=total, current=current)

    def add_error(self, detail: ErrorDetail) -> None:
        self.errors.append(detail)


JobHandler = Callable[[JobContext], Awaitable[BaseModel]]
```

- [ ] **Step 4: Implement `jobs/store.py`**

```python
"""Durable job rows ↔ public Job/JobDetail models (V1-09)."""

import json
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import JobRow
from research_engine_client.models import ErrorDetail, Job, JobDetail, JobProgress, JobStatus, JobType
from research_engine_client.models.jobs import JobResult

_RESULT = TypeAdapter(JobResult)
_ERRORS = TypeAdapter(list[ErrorDetail])


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None else None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_job(row: JobRow) -> Job:
    return Job(id=row.id, type=JobType(row.type), status=JobStatus(row.status),
               progress=JobProgress(done=row.progress_done, total=row.progress_total, current=row.progress_current),
               parent_id=row.parent_id, session_id=row.session_id, request=json.loads(row.request_json),
               result_ref=f"/v1/jobs/{row.id}" if row.result_json else None,
               errors=_ERRORS.validate_json(row.errors_json), created_at=_aware(row.created_at),  # type: ignore[arg-type]
               started_at=_aware(row.started_at), finished_at=_aware(row.finished_at))


class JobStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, type_: JobType, request: BaseModel, *, session_id: str | None) -> Job:
        row = JobRow(id=uuid.uuid4().hex, type=type_.value, status=JobStatus.QUEUED.value,
                     request_json=request.model_dump_json(), session_id=session_id, created_at=_now())
        async with AsyncSession(self._engine, expire_on_commit=False) as s:
            s.add(row)
            await s.commit()
        return to_job(row)

    async def row(self, job_id: str) -> JobRow | None:
        async with AsyncSession(self._engine) as s:
            return (await s.exec(select(JobRow).where(JobRow.id == job_id))).first()

    async def detail(self, job_id: str) -> JobDetail | None:
        r = await self.row(job_id)
        if r is None:
            return None
        result = _RESULT.validate_json(r.result_json) if r.result_json else None
        return JobDetail(job=to_job(r), result=result)

    async def claim(self, job_id: str) -> bool:
        async with AsyncSession(self._engine) as s:
            res = await s.exec(update(JobRow).where(col(JobRow.id) == job_id, col(JobRow.status) == "queued")  # type: ignore[call-overload]
                               .values(status="running", started_at=_now()))
            await s.commit()
            return (res.rowcount or 0) == 1

    async def set_progress(self, job_id: str, done: int, total: int, current: str | None) -> None:
        async with AsyncSession(self._engine) as s:
            await s.exec(update(JobRow).where(col(JobRow.id) == job_id)  # type: ignore[call-overload]
                         .values(progress_done=done, progress_total=total, progress_current=current))
            await s.commit()

    async def finish(self, job_id: str, status: JobStatus, *, result: BaseModel | None,
                     errors: list[ErrorDetail]) -> None:
        async with AsyncSession(self._engine) as s:
            await s.exec(update(JobRow).where(col(JobRow.id) == job_id).values(  # type: ignore[call-overload]
                status=status.value, finished_at=_now(), progress_current=None,
                result_json=result.model_dump_json() if result is not None else None,
                errors_json=_ERRORS.dump_json(errors).decode()))
            await s.commit()

    async def cancel_if_queued(self, job_id: str) -> bool:
        async with AsyncSession(self._engine) as s:
            res = await s.exec(update(JobRow).where(col(JobRow.id) == job_id, col(JobRow.status) == "queued")  # type: ignore[call-overload]
                               .values(status="cancelled", finished_at=_now(), errors_json=_ERRORS.dump_json(
                                   [ErrorDetail(code="job_cancelled", message="cancelled before start",  # type: ignore[arg-type]
                                                retryable=False)]).decode()))
            await s.commit()
            return (res.rowcount or 0) == 1

    async def ids_with_status(self, status: JobStatus) -> list[str]:
        async with AsyncSession(self._engine) as s:
            stmt = select(JobRow.id).where(JobRow.status == status.value).order_by(col(JobRow.created_at))
            return list((await s.exec(stmt)).all())

    async def recent(self, limit: int = 50) -> list[Job]:
        async with AsyncSession(self._engine) as s:
            rows = (await s.exec(select(JobRow).order_by(col(JobRow.created_at).desc()).limit(limit))).all()
        return [to_job(r) for r in rows]

    async def force_status(self, job_id: str, status: JobStatus) -> None:
        """Test and recovery helper."""
        async with AsyncSession(self._engine) as s:
            await s.exec(update(JobRow).where(col(JobRow.id) == job_id).values(status=status.value))  # type: ignore[call-overload]
            await s.commit()
```

- [ ] **Step 5: Implement `jobs/runner.py`**

```python
"""In-process asyncio job runner backed by the SQLite queue (V1-09)."""

import asyncio
import contextlib

import structlog
from pydantic import BaseModel

from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink
from research_engine_client.models import (
    BatchFetchRequest, ErrorCode, ErrorDetail, EventKind, Job, JobDetail, JobStatus, JobType, SearchReadRequest,
)

from .context import JobContext, JobHandler
from .store import JobStore

_log = structlog.get_logger("research_engine.jobs")
_REQUEST_MODELS: dict[JobType, type[BaseModel]] = {
    JobType.FETCH_BATCH: BatchFetchRequest, JobType.SEARCH_READ: SearchReadRequest}
MAX_WAIT_S = 60.0


class JobRunner:
    def __init__(self, store: JobStore, events: EventSink, *, workers: int, timeout_s: int) -> None:
        self._store, self._events = store, events
        self._workers_n, self._timeout = workers, timeout_s
        self._handlers: dict[JobType, JobHandler] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._done: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[BaseModel]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self.running: set[str] = set()

    def register(self, type_: JobType, handler: JobHandler) -> None:
        self._handlers[type_] = handler

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        for job_id in await self._store.ids_with_status(JobStatus.RUNNING):
            err = ErrorDetail(code=ErrorCode.INTERRUPTED, message="service restarted while job was running",
                              retryable=True)
            await self._store.finish(job_id, JobStatus.FAILED, result=None, errors=[err])
            await Emitter(self._events, job_id).error(EventKind.JOB_FAILED, "job interrupted by restart")
        for job_id in await self._store.ids_with_status(JobStatus.QUEUED):
            self._queue.put_nowait(job_id)
        self._workers = [asyncio.create_task(self._worker(), name=f"job-worker-{i}") for i in range(self._workers_n)]

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def submit(self, type_: JobType, request: BaseModel, *, session_id: str | None = None) -> Job:
        job = await self._store.create(type_, request, session_id=session_id)
        self._done[job.id] = asyncio.Event()
        await Emitter(self._events, job.id).info(EventKind.JOB_QUEUED, f"{type_.value} queued", type=type_.value)
        self._queue.put_nowait(job.id)
        return job

    async def get(self, job_id: str) -> JobDetail | None:
        return await self._store.detail(job_id)

    async def wait(self, job_id: str, timeout_s: float) -> JobDetail | None:
        detail = await self._store.detail(job_id)
        if detail is None or detail.job.status.is_terminal:
            return detail
        ev = self._done.setdefault(job_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ev.wait(), min(timeout_s, MAX_WAIT_S))
        return await self._store.detail(job_id)

    async def cancel(self, job_id: str) -> Job | None:
        if await self._store.cancel_if_queued(job_id):
            await Emitter(self._events, job_id).info(EventKind.JOB_CANCELLED, "cancelled before start")
            self._signal(job_id)
        elif (task := self._tasks.get(job_id)) is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._done.setdefault(job_id, asyncio.Event()).wait()
        detail = await self._store.detail(job_id)
        return detail.job if detail else None

    def _signal(self, job_id: str) -> None:
        self._done.setdefault(job_id, asyncio.Event()).set()

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if await self._store.claim(job_id):
                    await self._run(job_id)
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        row = await self._store.row(job_id)
        assert row is not None
        type_ = JobType(row.type)
        em = Emitter(self._events, job_id)
        request = _REQUEST_MODELS[type_].model_validate_json(row.request_json)
        ctx = JobContext(job_id=job_id, request=request, emitter=em,
                         _persist_progress=lambda d, t, c: self._store.set_progress(job_id, d, t, c))
        await em.info(EventKind.JOB_STARTED, f"{type_.value} started")
        self.running.add(job_id)
        task = asyncio.create_task(asyncio.wait_for(self._handlers[type_](ctx), self._timeout))
        self._tasks[job_id] = task
        try:
            result = await task
        except asyncio.CancelledError:
            ctx.errors.append(ErrorDetail(code=ErrorCode.JOB_CANCELLED, message="cancelled", retryable=False))
            await self._store.finish(job_id, JobStatus.CANCELLED, result=None, errors=ctx.errors)
            await em.info(EventKind.JOB_CANCELLED, "job cancelled")
        except TimeoutError:
            ctx.errors.append(ErrorDetail(code=ErrorCode.JOB_TIMEOUT, message=f"exceeded {self._timeout}s",
                                          retryable=True))
            await self._store.finish(job_id, JobStatus.FAILED, result=None, errors=ctx.errors)
            await em.error(EventKind.JOB_FAILED, "job timed out")
        except ServiceError as exc:
            ctx.errors.append(exc.detail)
            await self._store.finish(job_id, JobStatus.FAILED, result=None, errors=ctx.errors)
            await em.error(EventKind.JOB_FAILED, f"job failed: {exc.detail.message}")
        except Exception:
            _log.exception("job handler crashed", job_id=job_id)
            ctx.errors.append(ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="internal error", retryable=False))
            await self._store.finish(job_id, JobStatus.FAILED, result=None, errors=ctx.errors)
            await em.error(EventKind.JOB_FAILED, "job failed: internal error")
        else:
            status = JobStatus.PARTIAL if ctx.errors else JobStatus.DONE
            await self._store.finish(job_id, status, result=result, errors=ctx.errors)
            kind = EventKind.JOB_PARTIAL if ctx.errors else EventKind.JOB_DONE
            await em.info(kind, f"job {status.value} ({len(ctx.errors)} errors)", errors=len(ctx.errors))
        finally:
            self.running.discard(job_id)
            self._tasks.pop(job_id, None)
            self._signal(job_id)
```

Cancelling a running job: `task.cancel()` cancels the inner `wait_for` task, so `await task` in `_run` raises `CancelledError`. The **worker itself** isn't cancelled, so the handler records `cancelled` and the worker carries on. Confirm this with `test_cancel_running_and_queued`.

- [ ] **Step 6: Run the tests.** Expected: PASS. If one fails, use superpowers-extended-cc:systematic-debugging; async cancellation bugs are subtle.

- [ ] **Step 7: Commit.** `git commit -m "feat(jobs): durable job store and asyncio runner with cancel, timeout and recovery (V1-09)"`

---

### Task 4.5: Wire SQLite into the app; jobs API and maintenance

**Goal:** Switch the composition root to SQLite (events, cache, jobs), run the runner and a maintenance loop in the lifespan, and expose `GET /v1/jobs/{id}?wait=` and `DELETE /v1/jobs/{id}`.

**Files:**
- Modify: `packages/research_engine/src/research_engine/app.py`, `api/deps.py`, `testing.py`
- Create: `packages/research_engine/src/research_engine/api/jobs.py`
- Create: `packages/research_engine/src/research_engine/maintenance.py`
- Test: `tests/service/unit/test_api_jobs.py`, `tests/service/unit/test_maintenance.py`

**Acceptance Criteria:**
- [ ] `Services` gains `engine: AsyncEngine`, `jobs: JobRunner` and `job_store: JobStore`. `events` is typed `SqliteEventBus` and `cache` is `SqliteCache`. `build_test_services` uses `:memory:` SQLite, so tests exercise the real store.
- [ ] Startup:
  1. `init_db`;
  2. `runner.start()`;
  3. emit `system.startup` (with version and git sha);
  4. start `maintenance_loop`, which every hour prunes events older than `event_retention_days` and prunes expired cache entries.
- [ ] Shutdown: emit `system.shutdown`, stop the maintenance loop and runner, close the HTTP client, dispose the engine.
- [ ] `GET /v1/jobs/{id}` returns `Envelope[JobDetail]`. `?wait=N` (0–60; larger values give 422) waits for completion. An unknown id returns 404 with `not_found`.
- [ ] `DELETE /v1/jobs/{id}` returns `Envelope[Job]`, with status `cancelled` (or unchanged if the job is already terminal). An unknown id returns 404.
- [ ] `maintenance.run_once(services)` returns `{"events_pruned": n, "cache_pruned": m}` and is unit-tested.

**Verify:** `uv run pytest tests/service -v` → all pass (whole service suite, confirming nothing regressed after the swap)

**Steps:**

- [ ] **Step 1: Write the failing tests.** `test_api_jobs.py` registers a trivial handler on `app.state.services.jobs` for `FETCH_BATCH` (returns an empty `BatchFetchResult`), submits a job through `services.jobs.submit`, then checks the following:
  - `GET /v1/jobs/{id}?wait=5` gives `status=done`;
  - `GET /v1/jobs/nope` gives 404 `not_found`;
  - `GET /v1/jobs/{id}?wait=61` gives 422;
  - `DELETE` on a gated running job gives `cancelled`.

  `test_maintenance.py` inserts an old event and an expired cache row, then asserts that `run_once` returns `{"events_pruned": 1, "cache_pruned": 1}`.

```python
# tests/service/unit/test_api_jobs.py
import asyncio

import httpx

from research_engine.jobs.context import JobContext
from research_engine_client.models import BatchFetchRequest, BatchFetchResult, ErrorCode, JobType

REQ = BatchFetchRequest(urls=("https://a.example/",))


async def test_get_wait_and_404(app, client: httpx.AsyncClient) -> None:  # noqa: ANN001
    async def h(ctx: JobContext) -> BatchFetchResult:
        return BatchFetchResult(documents=[], failed=[])

    app.state.services.jobs.register(JobType.FETCH_BATCH, h)
    job = await app.state.services.jobs.submit(JobType.FETCH_BATCH, REQ)
    r = await client.get(f"/v1/jobs/{job.id}", params={"wait": 5})
    assert r.status_code == 200 and r.json()["data"]["job"]["status"] == "done"
    assert r.json()["data"]["result"]["kind"] == "fetch_batch"
    r404 = await client.get("/v1/jobs/nope")
    assert r404.status_code == 404 and r404.json()["errors"][0]["code"] == ErrorCode.NOT_FOUND
    assert (await client.get(f"/v1/jobs/{job.id}", params={"wait": 61})).status_code == 422


async def test_cancel(app, client: httpx.AsyncClient) -> None:  # noqa: ANN001
    started = asyncio.Event()

    async def h(ctx: JobContext) -> BatchFetchResult:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError

    app.state.services.jobs.register(JobType.FETCH_BATCH, h)
    job = await app.state.services.jobs.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    r = await client.delete(f"/v1/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
```

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement `api/jobs.py`**

```python
"""GET/DELETE /v1/jobs/{id} (V1-09)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from research_engine.errors import ServiceError
from research_engine_client.models import Envelope, ErrorCode, Job, JobDetail

from .deps import Services, get_services
from .envelope import ok

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _not_found(job_id: str) -> ServiceError:
    return ServiceError.of(ErrorCode.NOT_FOUND, f"job {job_id} not found", retryable=False, http_status=404)


@router.get("/{job_id}", response_model=Envelope[JobDetail])
async def get_job(request: Request, job_id: str, services: Annotated[Services, Depends(get_services)],
                  wait: Annotated[float, Query(ge=0, le=60, description="Seconds to wait for completion")] = 0
                  ) -> Envelope[JobDetail]:
    detail = await (services.jobs.wait(job_id, wait) if wait else services.jobs.get(job_id))
    if detail is None:
        raise _not_found(job_id)
    return ok(request, detail)


@router.delete("/{job_id}", response_model=Envelope[Job])
async def cancel_job(request: Request, job_id: str,
                     services: Annotated[Services, Depends(get_services)]) -> Envelope[Job]:
    job = await services.jobs.cancel(job_id)
    if job is None:
        raise _not_found(job_id)
    return ok(request, job)
```

- [ ] **Step 4: Implement `maintenance.py`**

```python
"""Periodic housekeeping: event retention and cache expiry (V1-10, V1-15)."""

import asyncio
import contextlib

import structlog

from research_engine.api.deps import Services

_log = structlog.get_logger("research_engine.maintenance")
INTERVAL_S = 3600


async def run_once(services: Services) -> dict[str, int]:
    events = await services.events.prune(services.settings.event_retention_days)
    cache = await services.cache.prune()
    return {"events_pruned": events, "cache_pruned": cache}


async def maintenance_loop(services: Services) -> None:
    while True:
        with contextlib.suppress(Exception):
            _log.info("maintenance", **await run_once(services))
        await asyncio.sleep(INTERVAL_S)
```

- [ ] **Step 5: Update `app.py`.** `build_services` creates the engine with `create_engine_for(settings.db_path)`, then `SqliteEventBus(engine)`, `SqliteCache(engine)`, `JobStore(engine)`, and `JobRunner(store, events, workers=settings.job_workers, timeout_s=settings.job_timeout_s)`. The lifespan follows the startup and shutdown order in the Acceptance Criteria. Include `jobs_api.router`. Mirror this in `testing.build_test_services`, which uses `:memory:`.

- [ ] **Step 6: Run the whole service suite.** Expected: PASS.

- [ ] **Step 7: Run the full step check** (Global Constraint 11).

- [ ] **Step 8: Commit.** `git commit -m "feat(jobs): SQLite-backed services, jobs API and maintenance loop (V1-09, V1-10, V1-15)"`

---

**End of step 4:** request code review, report to the owner with evidence, and **stop** for approval. After approval, run `git push`.
