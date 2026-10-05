"""Per-domain politeness: bounded concurrency and minimum spacing (V1-11)."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx

from research_engine.pipeline.urls import domain_of

MAX_DELAY_S = 30.0
DEFAULT_MAX_DOMAINS = 10_000


def limiter_key(url: str) -> str:
    """The per-domain key: the host httpx connects to (IDNA-2008 punycode), minus ``www.``.

    The SSRF guard and robots.txt use the same parser, so a robots crawl-delay set for an IDN
    host lands on the same slot that fetches it. Falls back to ``domain_of`` if unparseable.
    """
    try:
        host = httpx.URL(url).raw_host.decode("ascii").lower().rstrip(".")
    except (httpx.InvalidURL, UnicodeError):
        return domain_of(url)
    return host.removeprefix("www.")


class _DomainState:
    __slots__ = ("active", "delay", "last", "lock", "sem")

    def __init__(self, concurrency: int) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.delay: float | None = None
        self.active = 0  # slots held or waiting; a busy domain is never evicted


class DomainLimiter:
    """State is kept per domain in an LRU capped at ``max_domains`` (idle entries only)."""

    def __init__(
        self,
        concurrency: int,
        delay_s: float,
        max_domains: int = DEFAULT_MAX_DOMAINS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._concurrency = concurrency
        self._default_delay = delay_s
        self._max = max_domains
        self._states: OrderedDict[str, _DomainState] = OrderedDict()

    @property
    def tracked_domains(self) -> int:
        return len(self._states)

    def delay_for(self, domain: str) -> float:
        state = self._states.get(domain)
        if state is None or state.delay is None:
            return self._default_delay
        return state.delay

    def _state(self, domain: str) -> _DomainState:
        state = self._states.get(domain)
        if state is None:
            state = self._states[domain] = _DomainState(self._concurrency)
        else:
            self._states.move_to_end(domain)
        return state

    def _evict(self) -> None:
        if len(self._states) <= self._max:
            return
        for key in [k for k, s in self._states.items() if s.active == 0]:
            if len(self._states) <= self._max:
                break
            del self._states[key]

    def set_delay(self, domain: str, delay_s: float) -> None:
        state = self._state(domain)
        state.delay = min(max(delay_s, self._default_delay), MAX_DELAY_S)
        self._evict()

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        state = self._state(limiter_key(url))
        state.active += 1
        try:
            async with state.sem:
                async with state.lock:  # serialise the spacing decision per domain
                    delay = self._default_delay if state.delay is None else state.delay
                    wait = state.last + delay - self._clock()
                    if wait > 0:
                        await self._sleep(wait)
                    state.last = self._clock()
                yield
        finally:
            state.active -= 1
            self._evict()
