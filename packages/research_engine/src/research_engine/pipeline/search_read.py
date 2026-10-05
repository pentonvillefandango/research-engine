"""Job handlers: batch fetch (V1-07) and search-and-read (V1-08)."""

import asyncio
from typing import Protocol

import structlog
from pydantic import ValidationError
from research_engine_client.models import (
    BatchFetchRequest,
    BatchFetchResult,
    Document,
    ErrorCode,
    ErrorDetail,
    FailedUrl,
    FetchRequest,
    RankedDocument,
    SearchReadRequest,
    SearchReadResult,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

from research_engine.errors import ServiceError
from research_engine.jobs.context import JobContext

from .urls import fetch_cache_url

_log = structlog.get_logger("research_engine.pipeline.search_read")


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
            except Exception:
                # One page's bug (an extractor crash, say) must not fail its siblings or the
                # job (Global Constraint 7). Cancellation is a BaseException: it propagates.
                _log.exception("unexpected error fetching page", job_id=ctx.job_id, url=req.url)
                out[key] = ServiceError.of(
                    ErrorCode.INTERNAL_ERROR, "internal error", retryable=False, source=req.url
                )
            done += 1
            await ctx.progress(min(progress_offset + done, total), total, current=req.url)

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
    # Results are already deduplicated by the search canonicaliser, which is coarser than page
    # identity; dedupe again by page so one page never counts twice toward top_n.
    unique: dict[str, SearchResult] = {}
    for result in response.results:
        unique.setdefault(fetch_cache_url(result.url), result)
    candidates = list(unique.values())[: req.top_n * 2]
    documents: list[RankedDocument] = []
    failed: list[FailedUrl] = []
    attempted = 0
    idx = 0
    while len(documents) < req.top_n and idx < len(candidates):
        need = req.top_n - len(documents)
        wave = candidates[idx : idx + need]
        idx += len(wave)
        fetch_reqs: list[FetchRequest] = []
        wave_ok: list[SearchResult] = []
        for r in wave:
            try:
                fetch_reqs.append(
                    FetchRequest(
                        url=r.url,
                        mode=req.fetch.mode,
                        formats=req.fetch.formats,
                        use_cache=req.fetch.use_cache,
                        timeout_s=req.fetch.timeout_s,
                    )
                )
            except ValidationError:
                # The URL comes from a third-party search engine; skip it and back-fill.
                detail = ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="search result URL is not a fetchable http(s) URL",
                    retryable=False,
                    source=r.url[:200],
                )
                failed.append(FailedUrl(url=r.url[:200], error=detail))
                ctx.add_error(detail)
            else:
                wave_ok.append(r)
        outcomes = await _fetch_many(
            ctx,
            fetch,
            fetch_reqs,
            concurrency,
            progress_offset=1 + attempted,
            progress_total=total,
        )
        attempted += len(wave)
        for r in wave_ok:
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
