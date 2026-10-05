import asyncio

import httpx
from research_engine.errors import ServiceError
from research_engine.pipeline.search_read import run_batch_fetch
from research_engine_client.models import BatchFetchRequest, ErrorCode, JobType

from .helpers import FakeFetch, make_ctx


async def test_order_concurrency_partial() -> None:
    urls = tuple(f"https://s{i}.example/" for i in range(8))
    fake = FakeFetch(fail={urls[3]})
    progress: list[tuple[int, int, str | None]] = []
    c = make_ctx(BatchFetchRequest(urls=urls), progress)
    result = await run_batch_fetch(c, fake, concurrency=3)
    assert [d.url for d in result.documents] == [u for u in urls if u != urls[3]]
    assert [f.url for f in result.failed] == [urls[3]] and len(c.errors) == 1
    assert fake.peak <= 3 and progress[-1][:2] == (8, 8)
    assert all(job_id == "J" for _, job_id in fake.calls)
    assert {p[2] for p in progress} <= {*urls, None}


async def test_all_fail_raises() -> None:
    urls = ("https://a.example/", "https://b.example/")
    c = make_ctx(BatchFetchRequest(urls=urls), [])
    try:
        await run_batch_fetch(c, FakeFetch(fail=set(urls)), concurrency=2)
    except ServiceError as exc:
        assert (
            exc.detail.code is ErrorCode.FETCH_FAILED and "all 2 URLs failed" in exc.detail.message
        )
    else:
        raise AssertionError("expected failure")
    assert len(c.errors) == 2


async def test_dedupes_tracking_params_keeps_positions() -> None:
    urls = ("https://a.example/x?utm_source=1", "https://a.example/x")
    fake = FakeFetch()
    progress: list[tuple[int, int, str | None]] = []
    result = await run_batch_fetch(
        make_ctx(BatchFetchRequest(urls=urls), progress), fake, concurrency=2
    )
    assert len(fake.calls) == 1 and len(result.documents) == 2
    assert progress[-1][:2] == (2, 2)


async def test_distinct_pages_are_not_merged() -> None:
    urls = (
        "https://a.example/x?ref=main",
        "https://a.example/x?ref=dev",
        "https://a.example/x#frag",
    )
    fake = FakeFetch()
    result = await run_batch_fetch(make_ctx(BatchFetchRequest(urls=urls), []), fake, concurrency=3)
    fetched = {u for u, _ in fake.calls}
    assert "https://a.example/x?ref=main" in fetched and "https://a.example/x?ref=dev" in fetched
    assert len(result.documents) == 3


async def test_cancel_propagates_to_inflight_fetches() -> None:
    urls = tuple(f"https://s{i}.example/" for i in range(6))
    fake = FakeFetch(delay=30, saturate_at=3, cleanup_s=0.05)
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        run_batch_fetch(make_ctx(BatchFetchRequest(urls=urls), []), fake, concurrency=3)
    )
    await asyncio.wait_for(fake.saturated.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert fake.inflight == 0 and fake.cancelled == 3 and len(fake.calls) == 3
    assert fake.cleaned == 3  # the job task returned only after every fetch finished unwinding
    assert asyncio.all_tasks() - before == set()  # no orphaned fetch tasks


async def test_job_cancel_stops_fetches(app, client: httpx.AsyncClient) -> None:
    fake = FakeFetch(delay=30, saturate_at=2)
    runner = app.state.services.jobs
    runner.register(JobType.FETCH_BATCH, lambda ctx: run_batch_fetch(ctx, fake, concurrency=2))
    urls = [f"https://s{i}.example/" for i in range(5)]
    r = await client.post("/v1/fetch/batch", json={"urls": urls})
    job_id = r.json()["data"]["id"]
    await asyncio.wait_for(fake.saturated.wait(), 5)
    cancelled = await client.delete(f"/v1/jobs/{job_id}")
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert fake.inflight == 0 and fake.cancelled == 2


async def test_endpoint_202(app, client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/fetch/batch", json={"urls": ["https://blog.example/post"], "session_id": "sess1"}
    )
    assert r.status_code == 202 and r.headers["location"] == f"/v1/jobs/{r.json()['data']['id']}"
    assert r.json()["data"]["status"] == "queued"
    detail = (await client.get(r.headers["location"], params={"wait": 10})).json()["data"]
    assert detail["job"]["status"] == "done"
    assert detail["result"]["documents"][0]["url"] == "https://blog.example/post"
    assert detail["job"]["session_id"] == "sess1"
    too_many = await client.post(
        "/v1/fetch/batch", json={"urls": [f"https://a.example/{i}" for i in range(51)]}
    )
    assert too_many.status_code == 422
    assert too_many.json()["errors"][0]["code"] == ErrorCode.INVALID_REQUEST
    unauth = await client.post(
        "/v1/fetch/batch", json={"urls": ["https://a.example/"]}, headers={"X-API-Key": "x"}
    )
    assert unauth.status_code == 401


async def test_openapi_documents_batch(app, client: httpx.AsyncClient) -> None:
    op = (await client.get("/openapi.json")).json()["paths"]["/v1/fetch/batch"]["post"]
    assert "202" in op["responses"] and {"401", "422", "500"} <= set(op["responses"])
    assert op["requestBody"]["content"]["application/json"]["examples"]


async def test_unexpected_error_in_one_page_is_isolated() -> None:
    urls = tuple(f"https://s{i}.example/" for i in range(3))
    fake = FakeFetch(crash={urls[1]})
    c = make_ctx(BatchFetchRequest(urls=urls), [])
    result = await run_batch_fetch(c, fake, concurrency=3)
    assert [d.url for d in result.documents] == [urls[0], urls[2]]
    assert [f.url for f in result.failed] == [urls[1]]
    err = result.failed[0].error
    assert err.code is ErrorCode.INTERNAL_ERROR and err.message == "internal error"
    assert "boom" not in err.model_dump_json() and len(c.errors) == 1
