from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from research_engine.cache.base import Cache
from research_engine.cache.sqlite import SqliteCache
from research_engine.store.db import create_engine_for, init_db
from sqlalchemy.ext.asyncio import AsyncEngine


class Clock:
    t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine_for(":memory:")
    await init_db(eng)
    yield eng
    await eng.dispose()


async def test_ttl_upsert_persist_prune(engine: AsyncEngine) -> None:
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


async def test_satisfies_cache_protocol_and_counts_hits(engine: AsyncEngine) -> None:
    c: Cache = SqliteCache(engine, clock=Clock())
    await c.set("k", b"v", ttl_s=10)
    assert await c.get("k") == b"v"
    assert await c.get("other") is None
    assert (c.stats().hits, c.stats().misses) == (1, 1)
    assert c.stats().hit_rate == 0.5


async def test_upsert_extends_ttl_and_size(engine: AsyncEngine) -> None:
    clock = Clock()
    c = SqliteCache(engine, clock=clock)
    await c.set("a", b"123", ttl_s=5)
    await c.set("b", b"45", ttl_s=100)
    assert await c.size() == (2, 5)
    clock.t += 3
    await c.set("a", b"1234", ttl_s=5)  # new expiry: t+5
    clock.t += 4  # past the original expiry, within the new one
    assert await c.get("a") == b"1234"
    assert await c.size() == (2, 6)


async def test_prune_keeps_live_rows(engine: AsyncEngine) -> None:
    clock = Clock()
    c = SqliteCache(engine, clock=clock)
    await c.set("short", b"x", ttl_s=5)
    await c.set("long", b"y", ttl_s=500)
    clock.t += 10
    assert await c.prune() == 1
    assert await c.get("long") == b"y"
    assert await c.size() == (1, 1)


async def test_large_value_round_trips(tmp_path: Path) -> None:
    eng = create_engine_for(str(tmp_path / "c.sqlite"))
    try:
        await init_db(eng)
        c = SqliteCache(eng, clock=Clock())
        big = bytes(range(256)) * (2 * 1024 * 1024 // 256)
        await c.set("big", big, ttl_s=60)
        assert await SqliteCache(eng, clock=Clock()).get("big") == big
        assert await c.size() == (1, len(big))
    finally:
        await eng.dispose()
