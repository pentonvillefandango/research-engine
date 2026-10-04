"""In-process pub/sub with bounded, drop-oldest subscriber queues."""

import asyncio
from typing import Self

from research_engine_client.models import Event


class _Subscription:
    """Async iterator over one subscriber's queue.

    It is registered with the bus on construction, so events emitted before the
    first ``__anext__`` are not lost. It deregisters on ``aclose()``, on any
    exception (including cancellation) raised while waiting, and on garbage
    collection, so abandoning it never leaks a queue.
    """

    def __init__(self, bus: "InMemoryEventBus", max_queue: int) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=max_queue)
        self._closed = False
        bus._subs.add(self)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        try:
            item = await self.queue.get()
        except BaseException:
            self._close()
            raise
        if item is None:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._subs.discard(self)
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(None)  # wake any pending reader

    def __del__(self) -> None:
        self._bus._subs.discard(self)


class InMemoryEventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._max = max_queue
        self._subs: set[_Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    async def emit(self, event: Event) -> None:
        for sub in list(self._subs):
            q = sub.queue
            if q.full():
                q.get_nowait()  # drop oldest; never block the emitter
            q.put_nowait(event)

    def subscribe(self) -> _Subscription:
        """Register a subscriber now and return its async iterator.

        Events emitted after this call returns are delivered, even if iteration
        starts later.
        """
        return _Subscription(self, self._max)
