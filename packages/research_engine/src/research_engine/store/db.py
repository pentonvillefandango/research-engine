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
      awaiting work that opens another session: the second checkout fails fast with a pool
      ``TimeoutError`` after ``pool_timeout`` (2 s) instead of deadlocking.
    - The keeper means the data survives SQLAlchemy invalidating the pooled connection, which
      it does when a task is cancelled mid-query (the job runner cancels handlers by design).

    Both engines disable the pool's reset-on-return (``pool_reset_on_return=None``). That
    ROLLBACK is awaited while the connection is being checked in, and under SQLAlchemy 2.0
    a cancel landing on it invalidates the connection and re-raises *before* the pool record
    is checked back in: the slot then stays out until the orphaned connection proxy is
    garbage collected (never, while the cancelled task's traceback is referenced), so later
    checkouts time out. SQLAlchemy 2.1.3 fixes this in ``_finalize_fairy``, but sqlmodel
    pins ``SQLAlchemy<2.1``. The ROLLBACK is redundant here: ``Connection.close()`` already
    rolls back any open transaction itself (cancel-safe: it invalidates and checks in), and
    every DBAPI transaction is opened through a SQLAlchemy ``Transaction`` (no raw DBAPI use).
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
            pool_timeout=2,
            pool_reset_on_return=None,
            connect_args={"check_same_thread": False},
        )
        weakref.finalize(engine.sync_engine, keeper.close)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}", pool_reset_on_return=None)

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
