from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from research_engine.store.db import create_engine_for, init_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


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
