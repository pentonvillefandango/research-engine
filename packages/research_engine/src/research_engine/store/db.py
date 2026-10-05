"""Async SQLite engine with WAL (D9, V1-09/10/15)."""

import sqlite3
import uuid
import weakref
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlmodel import SQLModel

from . import tables  # noqa: F401 - registers tables on SQLModel.metadata


def create_engine_for(path: str) -> AsyncEngine:
    """Engine for a file path (WAL) or ``":memory:"`` (a private in-memory database).

    ``:memory:`` is a uniquely named shared-cache in-memory database behind a pool of one
    connection, kept alive by an idle keeper connection for the engine's lifetime:

    - Sessions are serialised (each waits for the previous one to close). Sharing a single
      connection between concurrent sessions (``StaticPool``) is unsafe: one session's close
      issues ROLLBACK on the connection and silently discards another session's
      not-yet-committed write. So on a ``:memory:`` engine, never hold a session open while
      awaiting work that opens another session.
    - The keeper means the data survives SQLAlchemy invalidating the pooled connection, which
      it does when a task is cancelled mid-query (the job runner cancels handlers by design).
    """
    in_memory = path == ":memory:"
    if in_memory:
        name = f"research-engine-{uuid.uuid4().hex}"
        uri = f"file:{name}?mode=memory&cache=shared"
        keeper = sqlite3.connect(uri, uri=True, check_same_thread=False)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{uri}&uri=true",
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=10,
            connect_args={"check_same_thread": False},
        )
        weakref.finalize(engine.sync_engine, keeper.close)
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
