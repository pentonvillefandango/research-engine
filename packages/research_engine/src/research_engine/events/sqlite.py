"""Persistent event log with live fan-out (V1-15)."""

import json
from datetime import UTC, datetime, timedelta

from research_engine_client.models import Event, EventKind, EventLevel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import EventRow

from .base import Subscription
from .memory import InMemoryEventBus

_ORDER = [EventLevel.DEBUG, EventLevel.INFO, EventLevel.WARNING, EventLevel.ERROR]
_MAX_LIMIT = 1000


def _to_model(row: EventRow) -> Event:
    # SQLite drops tzinfo: timestamps are stored as naive UTC, UTC is re-attached here.
    return Event(
        id=row.id,
        ts=row.ts.replace(tzinfo=UTC),
        job_id=row.job_id,
        level=EventLevel(row.level),
        kind=EventKind(row.kind),
        message=row.message,
        data=json.loads(row.data_json),
    )


class SqliteEventBus:
    """Persists every event, then publishes it (with its stored ``id``) to live subscribers."""

    def __init__(self, engine: AsyncEngine, live: InMemoryEventBus | None = None) -> None:
        self._engine = engine
        self._live = live or InMemoryEventBus()

    @property
    def subscriber_count(self) -> int:
        return self._live.subscriber_count

    async def emit(self, event: Event) -> None:
        row = EventRow(
            ts=event.ts.astimezone(UTC).replace(tzinfo=None),
            job_id=event.job_id,
            level=event.level.value,
            kind=event.kind.value,
            message=event.message,
            data_json=json.dumps(event.data, default=str),
        )
        async with AsyncSession(self._engine, expire_on_commit=False) as s:
            s.add(row)
            await s.commit()
        await self._live.emit(event.model_copy(update={"id": row.id}))

    def subscribe(self) -> Subscription:
        return self._live.subscribe()

    async def query(
        self,
        *,
        level: EventLevel | None = None,
        job_id: str | None = None,
        kind_prefix: str | None = None,
        text: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
        newest: bool = False,
    ) -> list[Event]:
        stmt = select(EventRow)
        if level is not None:
            allowed = [lv.value for lv in _ORDER[_ORDER.index(level) :]]
            stmt = stmt.where(col(EventRow.level).in_(allowed))
        if job_id is not None:
            stmt = stmt.where(EventRow.job_id == job_id)
        if kind_prefix:
            stmt = stmt.where(col(EventRow.kind).startswith(kind_prefix, autoescape=True))
        if text:
            # autoescape escapes %, _ and the escape char itself, so user text matches literally.
            stmt = stmt.where(col(EventRow.message).icontains(text, autoescape=True))
        if after_id is not None:
            stmt = stmt.where(col(EventRow.id) > after_id)
        # ``newest``: the last ``limit`` matches instead of the first; ascending order either way.
        order = col(EventRow.id).desc() if newest else col(EventRow.id)
        stmt = stmt.order_by(order).limit(max(0, min(limit, _MAX_LIMIT)))
        async with AsyncSession(self._engine) as s:
            rows = list((await s.exec(stmt)).all())
        return [_to_model(r) for r in (reversed(rows) if newest else rows)]

    async def tail(self, n: int) -> list[Event]:
        """The newest ``n`` events, in ascending id order."""
        stmt = select(EventRow).order_by(col(EventRow.id).desc()).limit(max(0, min(n, _MAX_LIMIT)))
        async with AsyncSession(self._engine) as s:
            rows = (await s.exec(stmt)).all()
        return [_to_model(r) for r in reversed(rows)]

    async def prune(self, older_than_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).replace(tzinfo=None)
        async with AsyncSession(self._engine) as s:
            result = await s.exec(delete(EventRow).where(col(EventRow.ts) < cutoff))
            await s.commit()
            return result.rowcount or 0
