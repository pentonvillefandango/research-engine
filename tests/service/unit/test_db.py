import asyncio
import contextlib
import gc
import sqlite3
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from research_engine.store.db import (
    ConnectionReturnedInTransactionError,
    RollbackOnCloseConnection,
    create_engine_for,
    init_db,
)
from research_engine.store.tables import CacheRow
from sqlalchemy import event, exc, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.fixture
async def mem_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine_for(":memory:")
    yield engine
    await engine.dispose()


async def test_wal_and_tables(tmp_path: Path) -> None:
    engine = create_engine_for(str(tmp_path / "sub" / "re.sqlite"))
    try:
        await init_db(engine)
        await init_db(engine)
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one()
            sync = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
            fks = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            names = {
                r[0]
                for r in await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
            indexes = {
                r[0]
                for r in await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='index'")
                )
            }
    finally:
        await engine.dispose()
    assert mode == "wal"
    assert busy == 5000
    assert sync == 1  # NORMAL
    assert fks == 1
    assert {"job", "event", "cache_entry"} <= names
    assert {
        "ix_event_ts",
        "ix_event_job_id",
        "ix_event_kind",
        "ix_job_status_created",
        "ix_cache_entry_expires_at",
    } <= indexes


async def test_memory_shared_between_sessions(mem_engine: AsyncEngine) -> None:
    await init_db(mem_engine)
    async with mem_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO cache_entry(key, value, expires_at) VALUES ('k', x'00', 1)")
        )
    async with mem_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cache_entry"))).scalar_one() == 1


async def test_memory_concurrent_sessions_do_not_lose_writes(mem_engine: AsyncEngine) -> None:
    """A reader session closing (and rolling back) must never undo another session's write.

    Regression: with one shared ``:memory:`` connection, a read session's close issued
    ROLLBACK on the connection while a concurrent session sat between its UPDATE and COMMIT,
    silently discarding the update even though its rowcount was 1.
    """
    await init_db(mem_engine)
    async with AsyncSession(mem_engine) as s:
        s.add(CacheRow(key="k", value=b"0", expires_at=0))
        await s.commit()
    updated = asyncio.Event()
    order: list[str] = []

    async def write() -> int:
        async with AsyncSession(mem_engine) as s:
            res = await s.exec(
                update(CacheRow).where(col(CacheRow.key) == "k").values(expires_at=1)
            )
            updated.set()
            await asyncio.sleep(0.05)  # mid-transaction: a concurrent session gets to run
            await s.commit()
            order.append("write committed")
            return res.rowcount or 0

    async def read() -> None:
        await updated.wait()
        async with AsyncSession(mem_engine) as s:
            await s.exec(select(CacheRow).limit(1))
        order.append("read closed")

    rowcount, _ = await asyncio.wait_for(asyncio.gather(write(), read()), 5)
    assert rowcount == 1
    async with AsyncSession(mem_engine) as s:
        row = (await s.exec(select(CacheRow).where(CacheRow.key == "k"))).one()
    assert row.expires_at == 1
    # sessions on the single in-memory connection are serialised, never interleaved
    assert order == ["write committed", "read closed"]


async def test_memory_survives_cancellation_mid_query(mem_engine: AsyncEngine) -> None:
    """SQLAlchemy invalidates a connection cancelled mid-query; the database must outlive it."""
    await init_db(mem_engine)

    async def touch() -> None:
        async with AsyncSession(mem_engine) as s:
            s.add(CacheRow(key=uuid.uuid4().hex, value=b"0", expires_at=0))
            await s.commit()

    for delay in (0, 0.0001, 0.0005, 0.001, 0.002):
        for _ in range(10):
            t = asyncio.create_task(touch())
            await asyncio.sleep(delay)
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
    async with AsyncSession(mem_engine) as s:
        assert (await s.exec(select(CacheRow))).all() is not None  # table still exists
    gc.collect()  # finalise the terminated connections now (see pyproject filterwarnings)


@pytest.mark.parametrize("kind", ["memory", "file"])
async def test_cancel_during_checkin_returns_connection_to_pool(kind: str, tmp_path: Path) -> None:
    """A task cancelled while its connection is being returned must still return it.

    Regression (D9, flaky CI pool ``TimeoutError``): after ``commit()`` the connection is
    checked in and the pool's reset-on-return awaited a ROLLBACK. A cancel landing on that
    await made SQLAlchemy 2.0's ``_finalize_fairy`` invalidate the connection and re-raise
    *before* ``checkin()``, so the pool slot stayed out until the orphaned fairy was garbage
    collected - never, while the cancelled task (and its traceback) is still referenced.
    The cancel is injected deterministically at the pool's ``reset`` hook.
    """
    engine = create_engine_for(":memory:" if kind == "memory" else str(tmp_path / "re.sqlite"))
    try:
        await init_db(engine)
        pool = engine.sync_engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)  # both engines: a real, bounded pool
        armed = False

        def cancel_at_checkin(*_: object) -> None:
            nonlocal armed
            if armed:
                armed = False
                task = asyncio.current_task()
                assert task is not None
                task.cancel()

        event.listen(pool, "reset", cancel_at_checkin)

        async def touch() -> None:
            nonlocal armed
            async with AsyncSession(engine) as s:
                s.add(CacheRow(key=uuid.uuid4().hex, value=b"0", expires_at=0))
                armed = True
                await s.commit()

        task = asyncio.create_task(touch())
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)  # the cancel did land
        # `task` (and so the exception's traceback) is still alive: no GC can rescue the slot
        assert pool.checkedout() == 0
        async with AsyncSession(engine) as s:
            assert len((await s.exec(select(CacheRow))).all()) == 1  # the commit stuck
    finally:
        await engine.dispose()


# A read that keeps the aiosqlite worker busy long enough to be cancelled mid-query.
_SLOW_QUERY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 3000000) "
    "SELECT count(*) FROM c"
)


def _engine(kind: str, tmp_path: Path) -> tuple[AsyncEngine, str | None]:
    if kind == "memory":
        return create_engine_for(":memory:"), None
    path = str(tmp_path / "re.sqlite")
    return create_engine_for(path), path


async def _other_writer_can_write(engine: AsyncEngine, path: str | None) -> bool:
    """Can an independent writer write right now (within ~1 s), with no ``gc.collect()``?"""
    key = uuid.uuid4().hex
    if path is not None:  # file: a separate connection, as another process would be

        def write() -> bool:
            c = sqlite3.connect(path, timeout=1.0)
            try:
                c.execute(
                    "INSERT INTO cache_entry(key, value, expires_at) VALUES (?, x'00', 0)", (key,)
                )
                c.commit()
                return True
            except sqlite3.OperationalError:  # database is locked
                return False
            finally:
                c.close()

        return await asyncio.to_thread(write)
    try:  # memory: shared-cache table locks fail at once (SQLITE_LOCKED), no busy wait
        async with AsyncSession(engine) as s:
            s.add(CacheRow(key=key, value=b"0", expires_at=0))
            await s.commit()
        return True
    except exc.OperationalError:  # database table is locked
        return False


@pytest.mark.parametrize("kind", ["memory", "file"])
async def test_cancel_mid_write_releases_the_write_lock(kind: str, tmp_path: Path) -> None:
    """Cancelling a task mid-query with a pending write must release SQLite's write lock.

    Regression (D9): the invalidated connection's sqlite3 handle was closed while the cancelled
    cursor's statement was still unfinalized, so ``sqlite3_close_v2`` left a "zombie" that kept
    the open write transaction - and the lock - until GC finalised the cursor. Every other
    writer got "database is locked" (file, after busy_timeout) / "database table is locked"
    (``:memory:``) meanwhile.
    """
    engine, path = _engine(kind, tmp_path)
    try:
        await init_db(engine)
        async with AsyncSession(engine) as s:
            s.add(CacheRow(key="committed", value=b"0", expires_at=0))
            await s.commit()
        slow_started = asyncio.Event()

        def flag_slow(_c: object, _cur: object, statement: str, *_: object) -> None:
            if statement == _SLOW_QUERY:
                slow_started.set()

        event.listen(engine.sync_engine, "before_cursor_execute", flag_slow)

        async def write_then_slow() -> None:
            async with AsyncSession(engine) as s:
                s.add(CacheRow(key="pending", value=b"0", expires_at=0))
                await s.flush()  # write transaction open, write lock held
                await (await s.connection()).execute(text(_SLOW_QUERY))

        task = asyncio.create_task(write_then_slow())
        await asyncio.wait_for(slow_started.wait(), 5)
        await asyncio.sleep(0.05)  # the slow query is running on the aiosqlite thread
        task.cancel()
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)
        # `task` (and its traceback, which holds the cancelled cursor) stays alive: no GC rescue
        assert await _other_writer_can_write(engine, path)
        async with AsyncSession(engine) as s:
            keys = {r.key for r in (await s.exec(select(CacheRow))).all()}
        assert "committed" in keys  # the database (and :memory:'s keeper) survived
        assert "pending" not in keys  # the cancelled write was rolled back
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["memory", "file"])
async def test_failed_commit_never_pools_an_open_transaction(kind: str, tmp_path: Path) -> None:
    """A COMMIT that fails must not return the connection to the pool mid-transaction.

    SQLite keeps the transaction open when COMMIT fails (here: a deferred FK violation), but
    SQLAlchemy considers it ended and checks the connection in as already reset - so it was
    pooled with the uncommitted rows and the write lock: the next session on it saw (and could
    commit) them, and other writers were locked out.
    """
    engine, path = _engine(kind, tmp_path)
    try:
        await init_db(engine)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE parent(id INTEGER PRIMARY KEY)"))
            await conn.execute(
                text(
                    "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER "
                    "REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
                )
            )
        with pytest.raises(exc.IntegrityError):
            async with AsyncSession(engine) as s:
                conn = await s.connection()
                await conn.execute(text("INSERT INTO child(parent_id) VALUES (999)"))
                s.add(CacheRow(key="uncommitted", value=b"0", expires_at=0))
                await s.commit()  # FOREIGN KEY constraint failed, at COMMIT
        pool = engine.sync_engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)
        assert pool.checkedout() == 0
        assert await _other_writer_can_write(engine, path)
        async with AsyncSession(engine) as s:
            conn = await s.connection()
            children = (await conn.execute(text("SELECT count(*) FROM child"))).scalar_one()
            keys = {r.key for r in (await s.exec(select(CacheRow))).all()}
        assert children == 0
        assert "uncommitted" not in keys
    finally:
        await engine.dispose()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SQLAlchemy _finalize_fairy skips checkin on cancel during invalidate (after the reset "
        "guard raises); still present in 2.0.54 and 2.1.3; see TODO in store/db.py"
    ),
)
@pytest.mark.parametrize("kind", ["memory", "file"])
async def test_cancel_during_guard_invalidation_returns_connection(
    kind: str, tmp_path: Path
) -> None:
    """Known residual (accepted for V1): the reset guard's invalidation can lose the pool slot.

    When ``_refuse_open_transaction`` raises, ``_finalize_fairy`` invalidates the connection
    and aiosqlite's terminate awaits a graceful close; a cancel landing there propagates out
    of the except block and skips ``checkin()``. Needs a failed COMMIT plus a precisely timed
    cancel, injected here from the pool ``invalidate`` hook. ``strict``: XPASS (a loud
    failure) once SQLAlchemy fixes it, as the cue to revisit the workaround.
    """
    engine, _ = _engine(kind, tmp_path)
    try:
        await init_db(engine)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE parent(id INTEGER PRIMARY KEY)"))
            await conn.execute(
                text(
                    "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER "
                    "REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
                )
            )

        def cancel_in_guard_invalidation(_c: object, _r: object, err: BaseException | None) -> None:
            if isinstance(err, ConnectionReturnedInTransactionError):
                task = asyncio.current_task()
                assert task is not None
                task.cancel()  # lands on terminate's graceful-close await

        event.listen(engine.sync_engine.pool, "invalidate", cancel_in_guard_invalidation)

        async def failed_commit() -> None:
            async with AsyncSession(engine) as s:
                conn = await s.connection()
                await conn.execute(text("INSERT INTO child(parent_id) VALUES (999)"))
                with contextlib.suppress(exc.IntegrityError):
                    await s.commit()

        task = asyncio.create_task(failed_commit())
        await asyncio.wait({task}, timeout=5)  # bounded: never hangs the suite
        assert task.done()
        pool = engine.sync_engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)
        assert pool.checkedout() == 0
    finally:
        await _reap_orphaned_tasks()
        await asyncio.wait_for(engine.dispose(), 5)


async def _reap_orphaned_tasks() -> None:
    """Bounded cleanup for the residual above: the interrupted graceful close leaves an
    orphaned SQLAlchemy ``_terminate_graceful_close`` task inside aiosqlite's ``close()``,
    re-awaiting a ``stop()`` future after the worker thread has already exited. It never
    finishes on its own and absorbs the single cancel ``asyncio.run`` teardown sends, so
    left alone it hangs the event loop's shutdown. Cancel repeatedly, never wait unbounded.
    """
    for _ in range(5):
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not others:
            return
        for t in others:
            t.cancel()
        await asyncio.wait(others, timeout=1)


def test_rollback_on_close_connection_releases_lock_despite_live_cursor(tmp_path: Path) -> None:
    """The sqlite3 connection class used by both engines: close() must end the transaction
    even while a cursor's statement is unfinalized (plain sqlite3 would leave a zombie)."""
    path = str(tmp_path / "z.sqlite")
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t(x)")
    setup.close()
    for factory, expect_locked in ((sqlite3.Connection, True), (RollbackOnCloseConnection, False)):
        conn = sqlite3.connect(path, factory=factory)
        conn.execute("INSERT INTO t VALUES (1)")  # write transaction open
        cur = conn.cursor()
        cur.execute("SELECT 1 UNION ALL SELECT 2")
        cur.fetchone()  # statement left mid-step, i.e. unfinalized
        conn.close()
        other = sqlite3.connect(path, timeout=0.1)
        try:
            if expect_locked:  # documents the sqlite3 behaviour being worked around
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("INSERT INTO t VALUES (2)")
            else:
                other.execute("INSERT INTO t VALUES (2)")
                other.commit()
        finally:
            other.close()
        del cur
        gc.collect()  # finalise the plain-sqlite3 zombie before the next round


async def test_memory_engines_are_isolated() -> None:
    a, b = create_engine_for(":memory:"), create_engine_for(":memory:")
    try:
        await init_db(a)
        async with a.begin() as conn:
            await conn.execute(
                text("INSERT INTO cache_entry(key, value, expires_at) VALUES ('k', x'00', 1)")
            )
        async with b.connect() as conn:
            tables = (
                await conn.execute(text("SELECT count(*) FROM sqlite_master WHERE type='table'"))
            ).scalar_one()
        assert tables == 0
    finally:
        await a.dispose()
        await b.dispose()
