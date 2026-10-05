"""Async SQLite engine with WAL (D9, V1-09/10/15)."""

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from . import tables  # noqa: F401 - registers tables on SQLModel.metadata


def create_engine_for(path: str) -> AsyncEngine:
    """Engine for a file path (WAL) or ``":memory:"`` (one shared connection via StaticPool)."""
    in_memory = path == ":memory:"
    if in_memory:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        if not in_memory:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
