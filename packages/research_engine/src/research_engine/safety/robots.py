"""robots.txt policy with Protego (V1-11). Failure semantics follow RFC 9309.

Redirects: following an on-origin redirect is allowed by RFC 9309, but V1 keeps it simple and
treats a redirect as allow-all (and never follows it). See ADR-0026.
"""

import asyncio
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from protego import Protego
from research_engine_client.models import ErrorCode, EventKind

from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.pipeline.urls import domain_of

from .http import build_clean_request
from .limiter import DomainLimiter
from .ssrf import SsrfGuard

_ALLOW_ALL = Protego.parse("")
_DISALLOW_ALL = Protego.parse("User-agent: *\nDisallow: /\n")
ERROR_TTL_S = 600
# RFC 9309 §2.5: parsers must handle at least 500 KiB; anything beyond the cap is ignored.
MAX_ROBOTS_BYTES = 512 * 1024
ROBOTS_TIMEOUT_S = 15.0
DEFAULT_MAX_ORIGINS = 10_000


def _origin_of(url: str) -> str:
    """Normalised origin (lower-case scheme/host, no userinfo, no default port)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise ServiceError.of(
            ErrorCode.FETCH_FAILED, f"unparseable URL {url!r}", retryable=False, source=url
        ) from exc
    scheme = parts.scheme.lower()
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}" + (f":{port}" if port not in (None, default) else "")


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
        self._cache: dict[str, tuple[float, Protego]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def cached_origins(self) -> int:
        return len(self._cache)

    async def check(self, url: str, em: Emitter | None = None) -> None:
        rules = await self._rules(_origin_of(url), em)
        if not rules.can_fetch(url, self._token):
            if em:
                await em.warning(
                    EventKind.ROBOTS_DISALLOWED, f"robots.txt disallows {url}", url=url
                )
            raise ServiceError.of(
                ErrorCode.ROBOTS_DISALLOWED,
                f"robots.txt disallows {url}",
                retryable=False,
                source=url,
                http_status=403,
            )

    async def _rules(self, origin: str, em: Emitter | None) -> Protego:
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:  # one fetch per origin, however many callers are waiting
            cached = self._cache.get(origin)
            if cached and cached[0] > self._clock():
                return cached[1]
            rules, ttl, status = await self._fetch(origin)
            self._cache.pop(origin, None)
            self._cache[origin] = (self._clock() + ttl, rules)
            self._evict()
            delay = rules.crawl_delay(self._token)
            if delay:
                self._limiter.set_delay(domain_of(origin), float(delay))
            if em:
                await em.debug(
                    EventKind.ROBOTS_FETCHED,
                    f"robots.txt for {origin}: {status}",
                    origin=origin,
                    status=status,
                )
            return rules

    def _evict(self) -> None:
        while len(self._cache) > self._max_origins:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
            lock = self._locks.get(oldest)
            if lock is not None and not lock.locked():
                del self._locks[oldest]

    async def _fetch(self, origin: str) -> tuple[Protego, int, str]:
        robots_url = f"{origin}/robots.txt"
        await self._guard.check(robots_url)
        request = build_clean_request(robots_url, {"User-Agent": self._ua}, self._timeout)
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self._client.send(
                    request, stream=True, follow_redirects=False, auth=None
                )
                try:
                    if resp.is_redirect:
                        return _ALLOW_ALL, self._ttl, "redirect (treated as allow)"
                    if 200 <= resp.status_code < 300:
                        text = await self._read_capped(resp)
                        rules = await asyncio.to_thread(Protego.parse, text)
                        return rules, self._ttl, str(resp.status_code)
                    if 400 <= resp.status_code < 500:
                        return _ALLOW_ALL, self._ttl, str(resp.status_code)
                    return _DISALLOW_ALL, ERROR_TTL_S, str(resp.status_code)
                finally:
                    await resp.aclose()
        except httpx.InvalidURL:  # httpx cannot parse a hostile redirect Location: a redirect
            return _ALLOW_ALL, self._ttl, "redirect (invalid location, treated as allow)"
        except (httpx.HTTPError, TimeoutError) as exc:
            return _DISALLOW_ALL, ERROR_TTL_S, f"error {type(exc).__name__}"

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
