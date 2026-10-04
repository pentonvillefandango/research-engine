"""In-process pub/sub with bounded, drop-oldest subscriber queues."""

import asyncio
from collections.abc import AsyncIterator

from research_engine_client.models import Event


class InMemoryEventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._max = max_queue
        self._subs: set[asyncio.Queue[Event]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    async def emit(self, event: Event) -> None:
        for q in list(self._subs):
            if q.full():
                q.get_nowait()  # drop oldest; never block the emitter
            q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        """Yield events emitted after subscribing.

        This is an async generator: its body, and so the queue registration,
        only runs on the first ``__anext__``. Callers must start iterating
        (for example ``await asyncio.sleep(0)`` after creating the consumer
        task) before emitting events they expect to receive.
        """
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)
