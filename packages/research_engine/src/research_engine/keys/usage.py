"""Per-request API usage log (V1-24, ADR-0028). ``record`` is sync and never blocks or raises;
a background task flushes batches every second. Stats and paging queries back the Keys page.

Rows hold only who, when, method, path, MCP tool name, status, duration and client IP: never a
body, query string, search term or URL."""

import asyncio
import contextlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import Integer, case, delete, func, insert
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import ApiRequestRow

_log = structlog.get_logger("research_engine.usage")
MAX_QUEUE = 10_000
FLUSH_INTERVAL_S = 1.0


def _naive(dt: datetime) -> datetime:
    """Naive UTC, as stored (see ``jobs/store.py``)."""
    return dt if dt.tzinfo is None else dt.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    ts: datetime
    caller_kind: str
    key_id: int | None
    caller_name: str
    method: str
    path: str
    tool: str | None
    status: int
    duration_ms: int
    client_ip: str


@dataclass(frozen=True, slots=True)
class KeyStats:
    last_24h: int
    last_30d: int
    last_used: datetime | None
    last_ip: str | None


def caller_id(kind: str, key_id: int | None) -> str:
    """``"env"``, ``"gui"`` or ``"key:<id>"``: the caller filter value the Keys page uses."""
    return f"key:{key_id}" if kind == "key" else kind


class UsageLog:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        max_queue: int = MAX_QUEUE,
        interval_s: float = FLUSH_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[RequestRecord] = asyncio.Queue(maxsize=max_queue)
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def record(self, rec: RequestRecord) -> None:
        """Queue one row. Never blocks: a full queue drops the row and counts it."""
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            self.dropped += 1

    async def flush(self) -> int:
        """Write everything queued in one transaction. Returns rows written; a database error
        is logged (the batch is lost) and never raised."""
        batch: list[RequestRecord] = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if self.dropped:
            _log.warning("request log dropped rows", dropped=self.dropped)
            self.dropped = 0
        if not batch:
            return 0
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(ApiRequestRow),
                    [asdict(r) | {"ts": _naive(r.ts)} for r in batch],
                )
        except Exception:
            _log.exception("request log flush failed", rows=len(batch))
            return 0
        return len(batch)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="usage-log")

    async def stop(self) -> None:
        """Stop the flusher, then write what is still queued (before the engine is disposed)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    async def prune(self, days: int) -> int:
        cutoff = _naive(datetime.now(UTC) - timedelta(days=days))
        async with self._engine.begin() as conn:
            res = await conn.execute(delete(ApiRequestRow).where(col(ApiRequestRow.ts) < cutoff))
        return res.rowcount

    async def key_stats(self) -> dict[str, KeyStats]:
        """Per caller seen in the last 30 days: 24-hour and 30-day counts, last use and the
        client IP of its latest request. One query: a window function ranks each caller's rows
        newest first, and the aggregate picks the IP of rank 1."""
        now = _naive(datetime.now(UTC))
        day, month = now - timedelta(days=1), now - timedelta(days=30)
        r = ApiRequestRow
        ranked = (
            sa_select(
                col(r.caller_kind).label("kind"),
                col(r.key_id).label("key_id"),
                col(r.ts).label("ts"),
                col(r.client_ip).label("ip"),
                func.row_number()
                .over(
                    partition_by=(col(r.caller_kind), col(r.key_id)),
                    order_by=(col(r.ts).desc(), col(r.id).desc()),
                )
                .label("rn"),
            )
            .where(col(r.ts) >= month)
            .subquery()
        )
        c = ranked.c
        stmt = sa_select(
            c.kind,
            c.key_id,
            func.sum(case((c.ts >= day, 1), else_=0)).cast(Integer),
            func.count(),
            func.max(c.ts),
            func.max(case((c.rn == 1, c.ip))),
        ).group_by(c.kind, c.key_id)
        async with self._engine.connect() as conn:
            result = (await conn.execute(stmt)).all()
        out: dict[str, KeyStats] = {}
        for kind, key_id, d, m, last, ip in result:
            last_used = last.replace(tzinfo=UTC) if isinstance(last, datetime) else None
            out[caller_id(str(kind), key_id)] = KeyStats(int(d or 0), int(m), last_used, ip)
        return out

    async def recent(
        self,
        *,
        caller: str | None = None,
        errors_only: bool = False,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ApiRequestRow]:
        """Rows newest first. ``caller`` is ``"env"``, ``"gui"`` or ``"key:<id>"`` (anything else
        means every caller); ``before_id`` pages backwards."""
        r = ApiRequestRow
        stmt = select(r).order_by(col(r.id).desc()).limit(limit)
        if caller in ("env", "gui"):
            stmt = stmt.where(r.caller_kind == caller)
        elif caller and caller.startswith("key:") and caller[4:].isdigit():
            stmt = stmt.where(r.caller_kind == "key", r.key_id == int(caller[4:]))
        if errors_only:
            stmt = stmt.where(col(r.status) >= 400)
        if before_id is not None:
            stmt = stmt.where(col(r.id) < before_id)
        async with AsyncSession(self._engine) as s:
            return list((await s.exec(stmt)).all())
