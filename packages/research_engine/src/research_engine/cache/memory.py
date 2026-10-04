"""Process-local TTL cache, used in tests and before step 4."""

import time
from collections.abc import Callable

from .base import CacheStats


class InMemoryCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[float, bytes]] = {}
        self._stats = CacheStats()

    async def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None or item[0] <= self._clock():
            self._data.pop(key, None)
            self._stats.misses += 1
            return None
        self._stats.hits += 1
        return item[1]

    async def set(self, key: str, value: bytes, ttl_s: int) -> None:
        self._data[key] = (self._clock() + ttl_s, value)

    def stats(self) -> CacheStats:
        return self._stats
