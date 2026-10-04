"""Event seam: every step emits structured events (V1-15)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from research_engine_client.models import Event, EventKind, EventLevel

_log = structlog.get_logger("research_engine.events")

# Named logger methods, not ``.log(level_int, ...)``: structlog's filtering
# ``log(self, level, event, ...)`` has a parameter called ``level``, which collides
# with our ``level=`` log field.
_LOG_METHODS = {
    EventLevel.DEBUG: "debug",
    EventLevel.INFO: "info",
    EventLevel.WARNING: "warning",
    EventLevel.ERROR: "error",
}


class EventSink(Protocol):
    async def emit(self, event: Event) -> None: ...


class Subscription(Protocol):
    """An open event subscription: iterate it, and close it when done."""

    def __aiter__(self) -> AsyncIterator[Event]: ...
    async def __anext__(self) -> Event: ...
    async def aclose(self) -> None: ...


class EventSubscriber(Protocol):
    def subscribe(self) -> Subscription: ...


class EventBus(EventSink, EventSubscriber, Protocol):
    """Sink plus subscriber: what the service container needs (memory now, SQLite in step 4)."""


class Emitter:
    def __init__(self, sink: EventSink, job_id: str | None = None) -> None:
        self._sink = sink
        self.job_id = job_id

    def bind(self, job_id: str | None) -> "Emitter":
        return Emitter(self._sink, job_id)

    async def _emit(
        self, level: EventLevel, kind: EventKind, message: str, data: dict[str, Any]
    ) -> None:
        event = Event(
            ts=datetime.now(UTC),
            job_id=self.job_id,
            level=level,
            kind=kind,
            message=message,
            data=data,
        )
        getattr(_log, _LOG_METHODS[level])(
            message,
            event_kind=kind.value,
            job_id=self.job_id,
            level=level.value,
            data=data,
        )
        await self._sink.emit(event)

    async def debug(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.DEBUG, kind, message, data)

    async def info(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.INFO, kind, message, data)

    async def warning(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.WARNING, kind, message, data)

    async def error(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.ERROR, kind, message, data)
