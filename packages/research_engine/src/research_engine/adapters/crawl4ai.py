"""Crawl4AI headless-browser fetcher via its REST API (V1-04). Sandbox stays ON (B3).

The request body never carries ``proxy``, ``proxy_config`` or ``extra_args``, and uses the
default ``browser_mode``: the sandboxed browser configuration is owned by the deployment.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from research_engine_client.models import ErrorCode, FetchMethod

from research_engine.errors import ServiceError
from research_engine.retry import retry
from research_engine.safety.ssrf import SsrfGuard

from .fetch import HopHook, RawPage

_SOURCE = "crawl4ai"
MAX_ERROR_CHARS = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _clean_error(raw: object) -> str:
    """Crawl4AI's error text, safe to put in an API error: one line, control chars removed."""
    text = " ".join(_CONTROL.sub(" ", str(raw or "")).split())
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 1] + "\u2026"
    return text or "unknown error"


class Crawl4AIFetcher:
    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.AsyncClient,
        guard: SsrfGuard,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client  # talks only to the internal Crawl4AI service
        self._guard = guard
        self._sleep = sleep

    async def fetch(self, url: str, *, timeout_s: float, on_hop: HopHook | None = None) -> RawPage:
        """``on_hop`` is not used: Chromium follows redirects inside Crawl4AI. The final
        URL is SSRF-checked here, and the orchestrator applies robots.txt to it."""
        target = str(await self._guard.check(url))  # send exactly the URL that was checked
        return await retry(
            lambda: self._crawl(url, target, timeout_s),
            attempts=2,
            sleep=self._sleep,
            # a timeout already spent the caller's budget; retrying would double it
            retry_if=lambda exc: exc.detail.code is not ErrorCode.UPSTREAM_TIMEOUT,
        )

    async def _crawl(self, url: str, target: str, timeout_s: float) -> RawPage:
        body = {
            "urls": [target],
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {"cache_mode": "bypass", "page_timeout": int(timeout_s * 1000)},
            },
        }
        try:
            resp = await self._client.post(
                f"{self._base}/crawl",
                json=body,
                timeout=timeout_s + 30,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.TimeoutException as exc:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"browser timed out on {target}",
                retryable=True,
                source=_SOURCE,
                http_status=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_ERROR,
                f"Crawl4AI unreachable: {type(exc).__name__}",
                retryable=True,
                source=_SOURCE,
            ) from exc
        if resp.status_code in (401, 403):
            raise ServiceError.of(
                ErrorCode.UPSTREAM_ERROR,
                "Crawl4AI rejected credentials; check CRAWL4AI_API_TOKEN",
                retryable=False,
                source=_SOURCE,
            )
        if resp.status_code >= 400:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_ERROR,
                f"Crawl4AI returned {resp.status_code}",
                retryable=resp.status_code >= 500,
                source=_SOURCE,
            )
        result = self._first_result(resp)
        if not result.get("success"):
            raise ServiceError.of(
                ErrorCode.FETCH_FAILED,
                f"browser fetch failed: {_clean_error(result.get('error_message'))}",
                retryable=True,
                source=target,
            )
        final = target
        redirected = result.get("redirected_url")
        if isinstance(redirected, str) and redirected:
            final = str(await self._guard.check(redirected))
        md = result.get("markdown")
        markdown = md.get("raw_markdown") if isinstance(md, dict) else md
        html = result.get("html") or ""
        status = result.get("status_code")
        return RawPage(
            url=url,
            final_url=final,
            status=status if isinstance(status, int) and status > 0 else 200,
            content_type="text/html",
            body=html.encode(),
            html=html,
            markdown=markdown if isinstance(markdown, str) else "",
            method=FetchMethod.BROWSER,
            redirects=[target] if final != target else [],
        )

    @staticmethod
    def _first_result(resp: httpx.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_ERROR,
                "Crawl4AI returned a non-JSON response",
                retryable=True,
                source=_SOURCE,
            ) from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        first = results[0] if isinstance(results, list) and results else None
        return first if isinstance(first, dict) else {}

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/health", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
