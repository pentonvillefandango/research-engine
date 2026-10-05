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


class RollbackOnCloseConnection(sqlite3.Connection):
    """``sqlite3`` connection whose ``close()`` first rolls back any open transaction.

    Plain ``close()`` (legacy transaction control) does no implicit rollback, and while any
    statement is still unfinalized - e.g. the cursor of a query cancelled mid-flight, kept
    alive by the cancelled task's traceback - ``sqlite3_close_v2`` only turns the handle into
    a "zombie" that keeps the open write transaction, and so SQLite's write lock, until GC
    finalises that cursor. Rolling back first ends the transaction (pending reads are aborted
    by SQLite) so the lock is released at close, deterministically.

    aiosqlite runs ``close()`` on its own worker thread, after any statement still executing,
    so this is thread-safe and never blocks the event loop. Wired in through the documented
    ``sqlite3.connect(factory=...)`` hook (aiosqlite forwards ``connect_args`` to it).
    """

    def close(self) -> None:
        try:
            self.rollback()  # no-op unless a transaction is open
        except sqlite3.Error:
            pass  # already closed / unusable: close() below still releases what it can
        finally:
            super().close()


class ConnectionReturnedInTransactionError(RuntimeError):
    """Raised by the pool ``reset`` guard so SQLAlchemy discards (invalidates) the connection."""


def _refuse_open_transaction(dbapi_conn: Any, _record: Any, _state: Any) -> None:
    """Pool ``reset`` guard: never pool a connection that is still inside a transaction.

    SQLAlchemy checks a connection in as "already reset" once its ``Transaction`` has ended,
    but SQLite keeps the transaction open when COMMIT itself fails (e.g. a deferred foreign
    key violation). Pooled like that, the next user would inherit the uncommitted rows and
    the write lock. Raising here makes ``_finalize_fairy`` invalidate the connection instead
    (whose close then rolls back: ``RollbackOnCloseConnection``). Synchronous - no await - so
    it adds no cancellation point to checkin.
    """
    if dbapi_conn.driver_connection.in_transaction:
        raise ConnectionReturnedInTransactionError(
            "connection returned to the pool inside a transaction; discarding it"
        )


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
    pins ``SQLAlchemy<2.1``. That ROLLBACK is normally redundant: ``Connection.close()`` rolls
    back a still-open ``Transaction`` itself (cancel-safe: it invalidates and checks in), and
    every DBAPI transaction is opened through a SQLAlchemy ``Transaction`` (no raw DBAPI use).
    The exception is a COMMIT that fails (e.g. deferred FK): SQLAlchemy treats the
    transaction as ended and skips every reset - with or without reset-on-return - while
    SQLite keeps it open. The synchronous ``reset`` guard (``_refuse_open_transaction``)
    catches that and has the connection invalidated rather than pooled.

    Both engines also build their sqlite3 connections as ``RollbackOnCloseConnection``, so
    closing an invalidated connection (a task cancelled mid-query, or the guard above)
    releases SQLite's write lock at once instead of when GC finalises the cancelled cursor.
    The ``:memory:`` keeper is a plain, never-pooled connection and is not affected.
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
            # TODO(sqlalchemy>=2.1.3): restore pool_reset_on_return (see docstring)
            pool_reset_on_return=None,
            connect_args={"check_same_thread": False, "factory": RollbackOnCloseConnection},
        )
        weakref.finalize(engine.sync_engine, keeper.close)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            # TODO(sqlalchemy>=2.1.3): restore pool_reset_on_return (see docstring)
            pool_reset_on_return=None,
            connect_args={"factory": RollbackOnCloseConnection},
        )
    event.listen(engine.sync_engine.pool, "reset", _refuse_open_transaction)

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
