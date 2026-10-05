"""Job handlers: batch fetch (V1-07) and search-and-read (V1-08)."""

import asyncio
from typing import Protocol

from research_engine_client.models import (
    BatchFetchRequest,
    BatchFetchResult,
    Document,
    ErrorCode,
    FailedUrl,
    FetchRequest,
)

from research_engine.errors import ServiceError
from research_engine.jobs.context import JobContext

from .urls import fetch_cache_url


class FetchesDocuments(Protocol):
    async def fetch(
        self, req: FetchRequest, *, job_id: str | None = None
    ) -> tuple[Document, bool]: ...


async def _fetch_many(
    ctx: JobContext,
    fetch: FetchesDocuments,
    requests: list[FetchRequest],
    concurrency: int,
    *,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> dict[str, Document | ServiceError]:
    """Fetch each distinct page once, with at most ``concurrency`` fetches in flight.

    Pages are identified by ``fetch_cache_url`` (the conservative page identity), not by the
    search canonicaliser, which would merge e.g. ``?ref=main`` and ``?ref=dev``. Returns
    ``fetch_cache_url -> Document | ServiceError``.

    The fetches run in a ``TaskGroup``: if the job is cancelled, every in-flight fetch is
    cancelled and awaited before this coroutine unwinds, so none is left orphaned.
    """
    unique: dict[str, FetchRequest] = {}
    for r in requests:
        unique.setdefault(fetch_cache_url(r.url), r)
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, Document | ServiceError] = {}
    done = 0
    total = progress_total if progress_total is not None else len(requests)

    async def one(key: str, req: FetchRequest) -> None:
        nonlocal done
        async with sem:
            try:
                out[key] = (await fetch.fetch(req, job_id=ctx.job_id))[0]
            except ServiceError as exc:
                out[key] = exc
            done += 1
            await ctx.progress(progress_offset + done, total, current=req.url)

    async with asyncio.TaskGroup() as group:
        for key, req in unique.items():
            group.create_task(one(key, req))
    return out


async def run_batch_fetch(
    ctx: JobContext, fetch: FetchesDocuments, *, concurrency: int
) -> BatchFetchResult:
    """Fetch every URL of a batch; one failing page never fails the job unless all fail."""
    req = ctx.request
    if not isinstance(req, BatchFetchRequest):
        raise TypeError("batch fetch handler got a different request type")
    requests = [
        FetchRequest(
            url=u,
            mode=req.mode,
            formats=req.formats,
            use_cache=req.use_cache,
            timeout_s=req.timeout_s,
        )
        for u in req.urls
    ]
    outcomes = await _fetch_many(ctx, fetch, requests, concurrency, progress_total=len(requests))
    documents: list[Document] = []
    failed: list[FailedUrl] = []
    for r in requests:
        o = outcomes[fetch_cache_url(r.url)]
        if isinstance(o, ServiceError):
            failed.append(FailedUrl(url=r.url, error=o.detail))
            ctx.add_error(o.detail)
        else:
            documents.append(o)
    # Duplicates are fetched once, so progress counted unique pages; close the gap.
    await ctx.progress(len(requests), len(requests))
    if not documents:
        raise ServiceError.of(
            ErrorCode.FETCH_FAILED, f"all {len(requests)} URLs failed", retryable=True
        )
    return BatchFetchResult(documents=documents, failed=failed)
