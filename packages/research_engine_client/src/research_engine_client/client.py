"""Async typed client for the Research Engine REST API (V1-13)."""

import asyncio
import time
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ValidationError

from . import __version__
from .models import (
    BatchFetchRequest,
    Document,
    EnginesResponse,
    Envelope,
    ErrorCode,
    ErrorDetail,
    FetchRequest,
    HealthReport,
    Job,
    JobDetail,
    Meta,
    SearchReadRequest,
    SearchRequest,
    SearchResponse,
    VersionInfo,
)

LONG_POLL_S = 30.0
"""Longest single server-side long-poll; the server allows up to 60 s."""
HTTP_MARGIN_S = 30.0
"""Added to a long-poll's wait to get that request's HTTP timeout."""
MIN_POLL_GAP_S = 0.25
"""A non-terminal poll that returns sooner than this (a server ignoring ``wait``) is not
repeated immediately, so ``wait_for_job`` can never busy-loop."""
BODY_SNIPPET_CHARS = 200


class ResearchEngineError(Exception):
    """A non-2xx response, or a 2xx with no data. Carries the typed ``errors`` of the envelope."""

    def __init__(self, status: int, errors: list[ErrorDetail], request_id: str | None) -> None:
        self.status, self.errors, self.request_id = status, errors, request_id
        super().__init__(f"HTTP {status}: " + "; ".join(f"{e.code}: {e.message}" for e in errors))

    @property
    def retryable(self) -> bool:
        return any(e.retryable for e in self.errors)


class ResearchEngineClient:
    """Typed async client. Use as ``async with ResearchEngineClient(url, api_key=...) as c``.

    ``base_url`` may end with or without ``/`` and may carry a path prefix (``https://h/re``).
    The API key is sent as ``X-API-Key`` and is never logged, printed or shown in ``repr``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            transport=transport,
            headers={
                "X-API-Key": api_key,
                "User-Agent": f"research-engine-client/{__version__}",
            },
        )
        self.last_meta: Meta | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        et: type[BaseException] | None,
        e: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _upstream_error(self, resp: httpx.Response) -> ResearchEngineError:
        """A body that is not a valid envelope (proxy page, crash): a typed ``upstream_error``."""
        snippet = resp.text.replace(self._api_key, "***") if self._api_key else resp.text
        message = snippet[:BODY_SNIPPET_CHARS] or resp.reason_phrase
        error = ErrorDetail(
            code=ErrorCode.UPSTREAM_ERROR,
            message=message,
            retryable=resp.status_code >= 500,
        )
        return ResearchEngineError(resp.status_code, [error], resp.headers.get("x-request-id"))

    async def _call[T: BaseModel](
        self,
        method: str,
        path: str,
        model: type[T],
        *,
        body: BaseModel | None = None,
        params: dict[str, float] | None = None,
        request_timeout_s: float | None = None,
        data_on_error: bool = False,
    ) -> T:
        """Send a request and return the envelope's ``data``.

        ``data_on_error`` returns ``data`` even from a non-2xx response (``/health`` answers
        503 with a full report); without data it still raises.
        """
        resp = await self._http.request(
            method,
            path,
            params=params,
            content=body.model_dump_json() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else None,
            timeout=request_timeout_s
            if request_timeout_s is not None
            else httpx.USE_CLIENT_DEFAULT,
        )
        try:
            env = Envelope[model].model_validate_json(resp.content)  # type: ignore[valid-type]
        except ValidationError:
            raise self._upstream_error(resp) from None
        self.last_meta = env.meta
        if env.data is not None and (not resp.is_error or data_on_error):
            return env.data
        errors = env.errors or [
            ErrorDetail(
                code=ErrorCode.UPSTREAM_ERROR,
                message=f"HTTP {resp.status_code} without data",
                retryable=resp.status_code >= 500,
            )
        ]
        raise ResearchEngineError(resp.status_code, errors, env.meta.request_id)

    async def search(self, req: SearchRequest) -> SearchResponse:
        return await self._call("POST", "/v1/search", SearchResponse, body=req)

    async def fetch(self, req: FetchRequest) -> Document:
        return await self._call("POST", "/v1/fetch", Document, body=req)

    async def fetch_batch(self, req: BatchFetchRequest) -> Job:
        return await self._call("POST", "/v1/fetch/batch", Job, body=req)

    async def search_read(self, req: SearchReadRequest) -> Job:
        return await self._call("POST", "/v1/search_read", Job, body=req)

    async def get_job(self, job_id: str, wait_s: float = 0) -> JobDetail:
        """With ``wait_s`` > 0 the server long-polls (max 60 s) until the job is terminal."""
        return await self._call(
            "GET",
            f"/v1/jobs/{job_id}",
            JobDetail,
            params={"wait": wait_s} if wait_s else None,
            request_timeout_s=wait_s + HTTP_MARGIN_S,
        )

    async def cancel_job(self, job_id: str) -> Job:
        return await self._call("DELETE", f"/v1/jobs/{job_id}", Job)

    async def wait_for_job(self, job_id: str, timeout_s: float = 900) -> JobDetail:
        """Long-poll in slices of at most 30 s until the job is terminal.

        Raises ``TimeoutError`` if it is not terminal after ``timeout_s`` seconds.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            started = time.monotonic()
            remaining = deadline - started
            detail = await self.get_job(job_id, wait_s=max(0.0, min(LONG_POLL_S, remaining)))
            if detail.job.status.is_terminal:
                return detail
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"job {job_id} not finished after {timeout_s}s")
            if now - started < MIN_POLL_GAP_S:
                await asyncio.sleep(min(MIN_POLL_GAP_S, deadline - now))

    async def engines(self) -> EnginesResponse:
        return await self._call("GET", "/v1/engines", EnginesResponse)

    async def health(self) -> HealthReport:
        """The health report, also when the service answers 503 (a dependency is down)."""
        return await self._call("GET", "/health", HealthReport, data_on_error=True)

    async def version(self) -> VersionInfo:
        return await self._call("GET", "/version", VersionInfo)
