import asyncio
import gc
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from research_engine.store.db import create_engine_for, init_db
from research_engine.store.tables import CacheRow
from sqlalchemy import event, text, update
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
