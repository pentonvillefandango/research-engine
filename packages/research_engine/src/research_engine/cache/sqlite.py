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
    """TTL cache keyed by string; expiry uses the injected wall clock (``time.time`` by default)."""

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
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": stmt.excluded.value, "expires_at": stmt.excluded.expires_at},
        )
        async with AsyncSession(self._engine) as s:
            await s.exec(stmt)
            await s.commit()

    def stats(self) -> CacheStats:
        return self._stats

    async def prune(self) -> int:
        """Delete expired rows; returns how many."""
        async with AsyncSession(self._engine) as s:
            res = await s.exec(delete(CacheRow).where(col(CacheRow.expires_at) <= self._clock()))
            await s.commit()
            return res.rowcount or 0

    async def size(self) -> tuple[int, int]:
        """``(rows, total value bytes)``, for the health strip."""
        stmt = select(func.count(), func.coalesce(func.sum(func.length(col(CacheRow.value))), 0))
        async with AsyncSession(self._engine) as s:
            n, total = (await s.exec(stmt)).one()
        return int(n), int(total)
