"""Static HTTP fetch: manual SSRF-checked redirects, size cap, type allow-list (V1-04, §8).

Known limit: httpx decodes gzip/deflate one network chunk at a time, so peak memory for a
decompression bomb is bounded by one chunk's expansion (about 1000x of <=64 KiB), not by
``max_bytes``. The decoded total is still capped at ``max_bytes`` and reading stops there.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urldefrag, urljoin

import httpx
from research_engine_client.models import ErrorCode, FetchMethod

from research_engine.errors import ServiceError
from research_engine.retry import retry
from research_engine.safety.http import build_clean_request
from research_engine.safety.ssrf import SsrfGuard

from .fetch import RawPage

MAX_REDIRECTS = 5
_ACCEPT = "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_-]+)""", re.IGNORECASE)
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


def _sniff(body: bytes) -> str | None:
    head = body[:1024].lstrip().lower()
    if head.startswith(b"%pdf-"):
        return "application/pdf"
    if head.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return None


def _decode(body: bytes, header_charset: str | None) -> str:
    charset = header_charset
    if not charset and (m := _META_CHARSET.search(body[:4096])):
        charset = m.group(1).decode("ascii", "ignore")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _media_type(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class StaticFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        guard: SsrfGuard,
        *,
        max_bytes: int,
        allowed_types: frozenset[str],
        user_agent: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._guard = guard
        self._max = max_bytes
        self._allowed = allowed_types
        self._ua = user_agent
        self._sleep = sleep

    async def fetch(self, url: str, *, timeout_s: float) -> RawPage:
        return await retry(lambda: self._fetch_once(url, timeout_s), attempts=3, sleep=self._sleep)

    async def _fetch_once(self, url: str, timeout_s: float) -> RawPage:
        current = url
        hops: list[str] = []
        seen = {urldefrag(url)[0]}
        try:
            async with asyncio.timeout(timeout_s):  # whole attempt, so slow-drip bodies are cut
                for _ in range(MAX_REDIRECTS + 1):
                    await self._guard.check(current)
                    request = build_clean_request(
                        current, {"User-Agent": self._ua, "Accept": _ACCEPT}, timeout_s
                    )
                    resp = await self._client.send(
                        request, stream=True, follow_redirects=False, auth=None
                    )
                    try:
                        if resp.is_redirect and "location" in resp.headers:
                            hops.append(current)
                            current = urljoin(current, resp.headers["location"].strip())
                            if urldefrag(current)[0] in seen:
                                raise ServiceError.of(
                                    ErrorCode.FETCH_FAILED,
                                    f"redirect loop fetching {url}",
                                    retryable=False,
                                    source=url,
                                )
                            seen.add(urldefrag(current)[0])
                            continue
                        self._check_status(resp, current)
                        ctype = self._check_declared_type(resp, current)
                        body = await self._read_capped(resp, current)
                        return self._build(url, current, resp, ctype, body, hops)
                    finally:
                        await resp.aclose()
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"timed out fetching {current}",
                retryable=True,
                source=current,
                http_status=504,
            ) from exc
        except httpx.InvalidURL as exc:  # e.g. a hostile Location header httpx cannot parse
            raise ServiceError.of(
                ErrorCode.FETCH_FAILED,
                f"invalid URL while fetching {current}: {exc}",
                retryable=False,
                source=current,
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceError.of(
                ErrorCode.FETCH_FAILED,
                f"network error fetching {current}: {exc}",
                retryable=True,
                source=current,
            ) from exc
        raise ServiceError.of(
            ErrorCode.FETCH_FAILED,
            f"too many redirects from {url}",
            retryable=False,
            source=url,
        )

    @staticmethod
    def _check_status(resp: httpx.Response, url: str) -> None:
        if resp.status_code < 300:
            return
        retryable = resp.status_code == 429 or resp.status_code >= 500
        raise ServiceError.of(
            ErrorCode.FETCH_FAILED,
            f"HTTP {resp.status_code} from {url}",
            retryable=retryable,
            source=url,
        )

    def _not_allowed(self, ctype: str, url: str) -> ServiceError:
        return ServiceError.of(
            ErrorCode.CONTENT_TYPE_NOT_ALLOWED,
            f"content type {ctype or 'unknown'!r} not allowed",
            retryable=False,
            source=url,
            http_status=415,
        )

    def _check_declared_type(self, resp: httpx.Response, url: str) -> str:
        """Reject a declared disallowed type before any body is read. '' means 'sniff later'."""
        ctype = _media_type(resp)
        if ctype and ctype not in self._allowed:
            raise self._not_allowed(ctype, url)
        return ctype

    async def _read_capped(self, resp: httpx.Response, url: str) -> bytes:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max:
            raise self._too_large(url)
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():  # decoded stream: gzip bombs are capped too
            size += len(chunk)
            if size > self._max:
                raise self._too_large(url)
            chunks.append(chunk)
        return b"".join(chunks)

    def _too_large(self, url: str) -> ServiceError:
        return ServiceError.of(
            ErrorCode.RESPONSE_TOO_LARGE,
            f"response exceeds {self._max} bytes",
            retryable=False,
            source=url,
            http_status=413,
        )

    def _build(
        self,
        url: str,
        final: str,
        resp: httpx.Response,
        declared_type: str,
        body: bytes,
        hops: list[str],
    ) -> RawPage:
        ctype = declared_type or _sniff(body) or ""
        if ctype not in self._allowed:
            raise self._not_allowed(ctype, final)
        html = _decode(body, resp.charset_encoding) if ctype in _TEXT_TYPES else None
        return RawPage(
            url=url,
            final_url=final,
            status=resp.status_code,
            content_type=ctype,
            body=body,
            html=html,
            markdown=None,
            method=FetchMethod.STATIC,
            redirects=hops,
        )

    async def health(self) -> bool:
        return True
