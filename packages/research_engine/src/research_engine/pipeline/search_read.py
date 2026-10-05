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
    RankedDocument,
    SearchReadRequest,
    SearchReadResult,
    SearchRequest,
    SearchResponse,
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


class SearchesWeb(Protocol):
    async def search(
        self, req: SearchRequest, *, job_id: str | None = None
    ) -> tuple[SearchResponse, bool]: ...


async def run_search_read(
    ctx: JobContext, search: SearchesWeb, fetch: FetchesDocuments, *, concurrency: int
) -> SearchReadResult:
    """Search, then read the top N results, back-filling past failures (max 2xN attempts).

    A failing search fails the job. Unlike batch fetch, zero readable documents is not a job
    failure: the search response and the per-URL errors are still a useful, explained answer
    for the calling agent, so the job ends ``partial`` (``ctx.errors`` is non-empty) instead
    of ``failed``. Zero search results is not an error at all (the job ends ``done``).
    """
    req = ctx.request
    if not isinstance(req, SearchReadRequest):
        raise TypeError("search_read handler got a different request type")
    total = req.top_n + 1
    await ctx.progress(0, total, current="search")
    response, _ = await search.search(req.search, job_id=ctx.job_id)
    await ctx.progress(1, total)
    # Search results are already deduplicated by the search canonicaliser: use them as given.
    candidates = response.results[: req.top_n * 2]
    documents: list[RankedDocument] = []
    failed: list[FailedUrl] = []
    idx = 0
    while len(documents) < req.top_n and idx < len(candidates):
        need = req.top_n - len(documents)
        wave = candidates[idx : idx + need]
        idx += len(wave)
        fetch_reqs = [
            FetchRequest(
                url=r.url,
                mode=req.fetch.mode,
                formats=req.fetch.formats,
                use_cache=req.fetch.use_cache,
                timeout_s=req.fetch.timeout_s,
            )
            for r in wave
        ]
        outcomes = await _fetch_many(
            ctx,
            fetch,
            fetch_reqs,
            concurrency,
            progress_offset=1 + len(documents),
            progress_total=total,
        )
        for r in wave:
            o = outcomes[fetch_cache_url(r.url)]
            if isinstance(o, ServiceError):
                failed.append(FailedUrl(url=r.url, error=o.detail))
                ctx.add_error(o.detail)
            else:
                documents.append(
                    RankedDocument(search_rank=r.rank, search_score=r.score, document=o)
                )
    documents.sort(key=lambda d: d.search_rank)
    await ctx.progress(total, total)
    return SearchReadResult(search=response, documents=documents, failed=failed)
