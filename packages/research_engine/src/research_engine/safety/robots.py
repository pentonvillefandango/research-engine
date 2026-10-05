"""robots.txt policy with Protego (V1-11). Failure semantics follow RFC 9309.

Redirects: following an on-origin redirect is allowed by RFC 9309, but V1 keeps it simple and
treats a redirect as allow-all (and never follows it). See ADR-0026.

An unavailable robots.txt (5xx, network error, timeout) disallows the whole origin for
``ERROR_TTL_S``, as RFC 9309 requires; the resulting ``robots_disallowed`` is ``retryable`` and
says why, while a real ``Disallow`` match is not retryable.
"""

import asyncio
import time
from collections.abc import Callable
from typing import NamedTuple

import httpx
from protego import Protego
from research_engine_client.models import ErrorCode, EventKind

from research_engine.errors import ServiceError
from research_engine.events.base import Emitter

from .http import build_clean_request
from .limiter import DomainLimiter, limiter_key
from .ssrf import SsrfGuard

_ALLOW_ALL = Protego.parse("")
_DISALLOW_ALL = Protego.parse("User-agent: *\nDisallow: /\n")
ERROR_TTL_S = 600
# RFC 9309 §2.5: parsers must handle at least 500 KiB; anything beyond the cap is ignored.
MAX_ROBOTS_BYTES = 512 * 1024
ROBOTS_TIMEOUT_S = 15.0
DEFAULT_MAX_ORIGINS = 10_000


class _Rules(NamedTuple):
    rules: Protego
    unavailable: str | None = None  # why robots.txt could not be read ("HTTP 503", ...)


def _origin_of(url: str) -> str:
    """Origin from httpx's parser (IDNA-2008 punycode host), the same one the guard checks."""
    try:
        u = httpx.URL(url)
        host = u.raw_host.decode("ascii").lower().rstrip(".")
        port = u.port
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ServiceError.of(
            ErrorCode.SSRF_BLOCKED,
            "blocked: unparseable URL",
            retryable=False,
            source=url,
            http_status=403,
        ) from exc
    if ":" in host:
        host = f"[{host}]"
    return f"{u.scheme}://{host}" + (f":{port}" if port is not None else "")


class _OriginLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0  # callers holding or waiting; the entry is dropped at zero uncached


class RobotsPolicy:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        limiter: DomainLimiter,
        guard: SsrfGuard,
        ttl_s: int = 3600,
        *,
        timeout_s: float = ROBOTS_TIMEOUT_S,
        max_origins: int = DEFAULT_MAX_ORIGINS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ua = user_agent
        self._token = user_agent.split("/", 1)[0].strip()
        self._limiter = limiter
        self._guard = guard
        self._ttl = ttl_s
        self._timeout = timeout_s
        self._max_origins = max_origins
        self._clock = clock
        self._cache: dict[str, tuple[float, _Rules]] = {}
        self._locks: dict[str, _OriginLock] = {}

    @property
    def tracked_locks(self) -> int:
        return len(self._locks)

    @property
    def cached_origins(self) -> int:
        return len(self._cache)

    async def check(self, url: str, em: Emitter | None = None) -> None:
        origin = _origin_of(url)
        rules, unavailable = await self._rules(origin, em)
        if not rules.can_fetch(url, self._token):
            if unavailable is None:
                message = f"robots.txt disallows {url}"
            else:
                message = (
                    f"robots.txt for {origin} was unavailable ({unavailable}), so the site is "
                    f"treated as disallowed for up to {ERROR_TTL_S // 60} minutes (RFC 9309); "
                    f"retry later: {url}"
                )
            if em:
                await em.warning(EventKind.ROBOTS_DISALLOWED, message, url=url)
            # Always 403: the refusal is this service's policy decision, not an upstream
            # failure; ``retryable`` tells callers whether waiting can change the answer.
            raise ServiceError.of(
                ErrorCode.ROBOTS_DISALLOWED,
                message,
                retryable=unavailable is not None,
                source=url,
                http_status=403,
            )

    async def _rules(self, origin: str, em: Emitter | None) -> _Rules:
        entry = self._locks.setdefault(origin, _OriginLock())
        entry.users += 1
        try:
            async with entry.lock:  # one fetch per origin, however many callers are waiting
                return await self._rules_locked(origin, em)
        finally:
            entry.users -= 1
            if entry.users == 0 and origin not in self._cache:
                self._locks.pop(origin, None)  # failed fetch cached nothing: don't leak the lock

    async def _rules_locked(self, origin: str, em: Emitter | None) -> _Rules:
        cached = self._cache.get(origin)
        if cached and cached[0] > self._clock():
            return cached[1]
        result, ttl, status = await self._fetch(origin)
        self._cache.pop(origin, None)
        self._cache[origin] = (self._clock() + ttl, result)
        self._evict()
        delay = result.rules.crawl_delay(self._token)
        if delay:
            self._limiter.set_delay(limiter_key(origin), float(delay))
        if em:
            await em.debug(
                EventKind.ROBOTS_FETCHED,
                f"robots.txt for {origin}: {status}",
                origin=origin,
                status=status,
            )
        return result

    def _evict(self) -> None:
        while len(self._cache) > self._max_origins:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
            entry = self._locks.get(oldest)
            if entry is not None and entry.users == 0:
                del self._locks[oldest]

    async def _fetch(self, origin: str) -> tuple[_Rules, int, str]:
        robots_url = f"{origin}/robots.txt"
        target = await self._guard.check(robots_url)  # request exactly the URL that was checked
        request = build_clean_request(target, {"User-Agent": self._ua}, self._timeout)
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self._client.send(
                    request, stream=True, follow_redirects=False, auth=None
                )
                try:
                    if resp.is_redirect:
                        return _Rules(_ALLOW_ALL), self._ttl, "redirect (treated as allow)"
                    if 200 <= resp.status_code < 300:
                        text = await self._read_capped(resp)
                        rules = await asyncio.to_thread(Protego.parse, text)
                        return _Rules(rules), self._ttl, str(resp.status_code)
                    if 400 <= resp.status_code < 500:
                        return _Rules(_ALLOW_ALL), self._ttl, str(resp.status_code)
                    why = f"HTTP {resp.status_code}"
                    return _Rules(_DISALLOW_ALL, why), ERROR_TTL_S, str(resp.status_code)
                finally:
                    await resp.aclose()
        except httpx.InvalidURL:  # httpx cannot parse a hostile redirect Location: a redirect
            return _Rules(_ALLOW_ALL), self._ttl, "redirect (invalid location, treated as allow)"
        except (httpx.HTTPError, TimeoutError) as exc:
            kind = "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
            why = f"network error: {kind}"
            return _Rules(_DISALLOW_ALL, why), ERROR_TTL_S, f"error {type(exc).__name__}"

    @staticmethod
    async def _read_capped(resp: httpx.Response) -> str:
        """Read at most MAX_ROBOTS_BYTES of the decoded body and ignore the rest."""
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) >= MAX_ROBOTS_BYTES:
                del buf[MAX_ROBOTS_BYTES:]
                break
        try:
            return bytes(buf).decode(resp.charset_encoding or "utf-8", errors="replace")
        except LookupError:
            return bytes(buf).decode("utf-8", errors="replace")
