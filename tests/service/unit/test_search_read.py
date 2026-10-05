import asyncio

import httpx
import pytest
from research_engine.errors import ServiceError
from research_engine.pipeline.search_read import run_search_read
from research_engine.testing import STATIC_PAGES
from research_engine_client.models import (
    ErrorCode,
    JobType,
    SearchReadRequest,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

from .helpers import FakeFetch, make_ctx


class FakeSearch:
    def __init__(self, n: int, fail: bool = False, urls: dict[int, str] | None = None) -> None:
        self.n, self.fail = n, fail
        self.urls = urls or {}
        self.job_ids: list[str | None] = []

    async def search(
        self, req: SearchRequest, *, job_id: str | None = None
    ) -> tuple[SearchResponse, bool]:
        self.job_ids.append(job_id)
        if self.fail:
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "down", retryable=True)
        results = [
            SearchResult(
                rank=i,
                url=self.urls.get(i, f"https://r{i}.example/"),
                canonical_url=f"https://r{i}.example/",
                title="t",
                snippet="s",
                domain=f"r{i}.example",
                engines=["a"],
                score=1 / i,
            )
            for i in range(1, self.n + 1)
        ]
        return SearchResponse(query=req.query, results=results), False


async def test_backfill_and_ranking() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=3)
    fetch = FakeFetch(fail={"https://r2.example/"})
    progress: list[tuple[int, int, str | None]] = []
    ctx = make_ctx(req, progress)
    search = FakeSearch(10)
    res = await run_search_read(ctx, search, fetch, concurrency=3)
    assert [d.search_rank for d in res.documents] == [1, 3, 4]
    assert [f.url for f in res.failed] == ["https://r2.example/"] and len(ctx.errors) == 1
    assert len(fetch.calls) == 4
    assert search.job_ids == ["J"] and all(j == "J" for _, j in fetch.calls)
    assert progress[0] == (0, 4, "search") and progress[1][:2] == (1, 4)
    assert progress[-1][:2] == (4, 4)


async def test_documents_sorted_by_rank_not_completion() -> None:
    class Slow(FakeFetch):
        async def fetch(self, req, *, job_id=None):  # type: ignore[no-untyped-def]
            self.delay = 0.05 if req.url == "https://r1.example/" else 0.0
            return await super().fetch(req, job_id=job_id)

    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=3)
    res = await run_search_read(make_ctx(req, []), FakeSearch(5), Slow(), concurrency=3)
    assert [d.search_rank for d in res.documents] == [1, 2, 3]
    assert [d.search_score for d in res.documents] == [1.0, 0.5, pytest.approx(1 / 3)]


async def test_cap_attempts() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=2)
    fetch = FakeFetch(fail={f"https://r{i}.example/" for i in range(1, 11)})
    ctx = make_ctx(req, [])
    res = await run_search_read(ctx, FakeSearch(10), fetch, concurrency=2)
    assert len(fetch.calls) == 4 and res.documents == []
    assert len(res.failed) == 4 and len(ctx.errors) == 4  # job ends partial, not failed


async def test_concurrency_bound() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=10)
    fetch = FakeFetch()
    await run_search_read(make_ctx(req, []), FakeSearch(20), fetch, concurrency=3)
    assert fetch.peak <= 3


async def test_zero_results_done() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"))
    ctx = make_ctx(req, [])
    res = await run_search_read(ctx, FakeSearch(0), FakeFetch(), concurrency=2)
    assert res.documents == [] and res.failed == [] and ctx.errors == []


async def test_search_failure_raises() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"))
    fetch = FakeFetch()
    with pytest.raises(ServiceError):
        await run_search_read(make_ctx(req, []), FakeSearch(5, fail=True), fetch, concurrency=2)
    assert fetch.calls == []


async def test_cancel_propagates_to_inflight_fetches() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=4)
    fetch = FakeFetch(delay=30, saturate_at=2, cleanup_s=0.05)
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        run_search_read(make_ctx(req, []), FakeSearch(8), fetch, concurrency=2)
    )
    await asyncio.wait_for(fetch.saturated.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and fetch.inflight == 0 and fetch.cancelled == 2
    assert fetch.cleaned == 2
    assert asyncio.all_tasks() - before == set()


async def test_progress_never_goes_backwards() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=2)
    fetch = FakeFetch(fail={f"https://r{i}.example/" for i in range(1, 11)})
    progress: list[tuple[int, int, str | None]] = []
    await run_search_read(make_ctx(req, progress), FakeSearch(10), fetch, concurrency=2)
    done = [p[0] for p in progress]
    assert done == sorted(done) and progress[-1][:2] == (3, 3)
    assert {p[1] for p in progress} == {3}


async def test_unexpected_error_in_one_page_is_isolated() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=3)
    fetch = FakeFetch(crash={"https://r2.example/"})
    ctx = make_ctx(req, [])
    res = await run_search_read(ctx, FakeSearch(10), fetch, concurrency=3)
    assert [d.search_rank for d in res.documents] == [1, 3, 4]
    assert [f.url for f in res.failed] == ["https://r2.example/"]
    assert res.failed[0].error.code is ErrorCode.INTERNAL_ERROR and len(ctx.errors) == 1


async def test_invalid_candidate_url_is_skipped_and_backfilled() -> None:
    long_url = "https://r2.example/" + "a" * 5000
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=3)
    fetch = FakeFetch()
    ctx = make_ctx(req, [])
    res = await run_search_read(ctx, FakeSearch(10, urls={2: long_url}), fetch, concurrency=3)
    assert [d.search_rank for d in res.documents] == [1, 3, 4]
    assert len(res.failed) == 1 and long_url.startswith(res.failed[0].url)
    assert res.failed[0].error.code is ErrorCode.INVALID_REQUEST and len(ctx.errors) == 1
    assert long_url not in [u for u, _ in fetch.calls]


async def test_duplicate_pages_count_once_toward_top_n() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=3)
    fetch = FakeFetch()
    # r2 is the same page as r1 (tracking param only); ranks 1, 3, 4 are distinct pages.
    search = FakeSearch(10, urls={2: "https://r1.example/?utm_source=x"})
    res = await run_search_read(make_ctx(req, []), search, fetch, concurrency=3)
    assert [d.search_rank for d in res.documents] == [1, 3, 4]
    assert len(fetch.calls) == 3


async def test_endpoint_202(app, client: httpx.AsyncClient, monkeypatch) -> None:
    monkeypatch.setitem(STATIC_PAGES, "https://result1.example/1", "article.html")
    body = {
        "search": {"query": "vector db", "depth": "quick"},
        "top_n": 2,
        "session_id": "sess1",
    }
    r = await client.post("/v1/search_read", json=body)
    assert r.status_code == 202 and r.headers["location"] == f"/v1/jobs/{r.json()['data']['id']}"
    assert (
        r.json()["data"]["type"] == JobType.SEARCH_READ and r.json()["data"]["status"] == "queued"
    )
    detail = (await client.get(r.headers["location"], params={"wait": 20})).json()["data"]
    assert detail["job"]["status"] == "partial" and detail["job"]["session_id"] == "sess1"
    result = detail["result"]
    assert result["kind"] == "search_read" and len(result["search"]["results"]) > 0
    assert [d["search_rank"] for d in result["documents"]] == [1]
    assert len(result["failed"]) == 3  # result2..4 are not in the fixture site: 2*top_n tries
    bad = await client.post("/v1/search_read", json={"search": {"query": "x"}, "top_n": 99})
    assert bad.status_code == 422


async def test_openapi_documents_search_read(app, client: httpx.AsyncClient) -> None:
    op = (await client.get("/openapi.json")).json()["paths"]["/v1/search_read"]["post"]
    assert "202" in op["responses"] and {"401", "422", "500"} <= set(op["responses"])
    assert op["requestBody"]["content"]["application/json"]["examples"]
    assert op["description"]
