import asyncio

import httpx
from research_engine.jobs.context import JobContext
from research_engine_client.models import BatchFetchRequest, BatchFetchResult, ErrorCode, JobType

REQ = BatchFetchRequest(urls=("https://a.example/",))


async def test_get_wait_and_404(app, client: httpx.AsyncClient) -> None:
    async def h(ctx: JobContext) -> BatchFetchResult:
        return BatchFetchResult(documents=[], failed=[])

    app.state.services.jobs.register(JobType.FETCH_BATCH, h)
    job = await app.state.services.jobs.submit(JobType.FETCH_BATCH, REQ)
    r = await client.get(f"/v1/jobs/{job.id}", params={"wait": 5})
    assert r.status_code == 200 and r.json()["data"]["job"]["status"] == "done"
    assert r.json()["data"]["result"]["kind"] == "fetch_batch"
    r404 = await client.get("/v1/jobs/nope")
    assert r404.status_code == 404 and r404.json()["errors"][0]["code"] == ErrorCode.NOT_FOUND
    assert (await client.get(f"/v1/jobs/{job.id}", params={"wait": 61})).status_code == 422


async def test_wait_bounds_are_invalid_request(app, client: httpx.AsyncClient) -> None:
    for wait in (-1, 61):
        r = await client.get("/v1/jobs/abc123", params={"wait": wait})
        assert r.status_code == 422
        assert r.json()["errors"][0]["code"] == ErrorCode.INVALID_REQUEST


async def test_unknown_and_malformed_ids_are_404(app, client: httpx.AsyncClient) -> None:
    for bad in ("0" * 32, "g" * 8, "A" * 8, "a" * 65, "x%20y", "caf%C3%A9"):
        for method in (client.get, client.delete):
            r = await method(f"/v1/jobs/{bad}")
            assert r.status_code == 404, (method, bad)
            assert r.json()["errors"][0]["code"] == ErrorCode.NOT_FOUND


async def test_malformed_id_does_not_touch_db(app, client: httpx.AsyncClient) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("DB touched")

    jobs = app.state.services.jobs
    jobs.get, jobs.wait, jobs.cancel = boom, boom, boom
    assert (await client.get("/v1/jobs/NOPE", params={"wait": 1})).status_code == 404
    assert (await client.delete("/v1/jobs/NOPE")).status_code == 404


async def test_cancel(app, client: httpx.AsyncClient) -> None:
    started = asyncio.Event()

    async def h(ctx: JobContext) -> BatchFetchResult:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError

    app.state.services.jobs.register(JobType.FETCH_BATCH, h)
    job = await app.state.services.jobs.submit(JobType.FETCH_BATCH, REQ)
    await asyncio.wait_for(started.wait(), 2)
    r = await client.delete(f"/v1/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"


async def test_delete_terminal_job_unchanged(app, client: httpx.AsyncClient) -> None:
    async def h(ctx: JobContext) -> BatchFetchResult:
        return BatchFetchResult(documents=[], failed=[])

    app.state.services.jobs.register(JobType.FETCH_BATCH, h)
    job = await app.state.services.jobs.submit(JobType.FETCH_BATCH, REQ)
    await client.get(f"/v1/jobs/{job.id}", params={"wait": 5})
    r = await client.delete(f"/v1/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["data"]["status"] == "done"


async def test_jobs_require_api_key(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost"
    ) as c:
        assert (await c.get("/v1/jobs/abc")).status_code == 401


async def test_openapi_documents_jobs(app, client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    path = spec["paths"]["/v1/jobs/{job_id}"]
    for method in ("get", "delete"):
        assert {"401", "404", "422", "500"} <= set(path[method]["responses"])
        assert path[method]["description"]
    assert "example" in path["get"]["responses"]["200"]["content"]["application/json"]


def test_get_example_matches_model() -> None:
    from research_engine.api.jobs import JOB_EXAMPLE
    from research_engine_client.models import Envelope, JobDetail

    detail = Envelope[JobDetail].model_validate(JOB_EXAMPLE).data
    assert detail is not None and detail.job.status.value == "done"
