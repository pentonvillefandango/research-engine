"""Acceptance criterion 3 (V1-14): every real response validates against its published schema.

Each public REST endpoint of the test app is called (successes, long-poll, batch and the error
paths), and the real JSON body is validated with ``jsonschema`` against the file exported to
``schemas/``, not against the Pydantic model. ``/v1/schemas/{name}`` success is the one
documented non-envelope (raw ``application/schema+json``); its 404 is still an envelope.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
from fastapi import FastAPI
from research_engine.testing import BLOCKED_HOST, STATIC_PAGES

from tests.conftest import TEST_API_KEY

ROOT = Path(__file__).resolve().parents[3]
EXEMPT_SUCCESS = {("GET", "/v1/schemas/{name}")}
ERROR = "Envelope_NoneType_"

DOC_URL = "https://blog.example/post"
PDF_URL = "https://docs.example/sample.pdf"


@cache
def validator(name: str) -> jsonschema.Draft202012Validator:
    path = ROOT / "schemas" / f"{name}.json"
    assert path.exists(), f"no exported schema {path.name}"
    schema = json.loads(path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def check(r: httpx.Response, status: int, schema: str) -> dict[str, Any]:
    assert r.status_code == status, (r.status_code, r.text[:500])
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    validator(schema).validate(body)
    return body


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the fake fetcher serve some of the fake search results, so search_read has documents
    as well as failures."""
    for i in range(1, 4):
        monkeypatch.setitem(STATIC_PAGES, f"https://result{i}.example/1", "article.html")


async def _covered_success_routes(client: httpx.AsyncClient) -> set[tuple[str, str]]:
    spec = (await client.get("/openapi.json")).json()
    return {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        if path.startswith("/v1") or path in ("/health", "/version")
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    }


async def test_every_success_response_validates(
    app: FastAPI, client: httpx.AsyncClient, served: None
) -> None:
    seen: set[tuple[str, str]] = set()

    def hit(method: str, template: str) -> None:
        seen.add((method, template))

    check(await client.get("/health"), 200, "Envelope_HealthReport_")
    hit("GET", "/health")
    check(await client.get("/version"), 200, "Envelope_VersionInfo_")
    hit("GET", "/version")

    names = check(await client.get("/v1/schemas"), 200, "Envelope_list_str__")
    assert "Envelope_NoneType_" in names["data"] and "Envelope_list_str__" in names["data"]
    hit("GET", "/v1/schemas")

    check(await client.get("/v1/engines"), 200, "Envelope_EnginesResponse_")
    hit("GET", "/v1/engines")

    for body in ({"query": "python asyncio"}, {"query": "x", "intent": "news", "depth": "quick"}):
        check(await client.post("/v1/search", json=body), 200, "Envelope_SearchResponse_")
    # a cache hit is a second, distinct response shape (meta.cache_hit true)
    cached = check(
        await client.post("/v1/search", json={"query": "python asyncio"}),
        200,
        "Envelope_SearchResponse_",
    )
    assert cached["meta"]["cache_hit"] is True
    hit("POST", "/v1/search")

    for url in (DOC_URL, PDF_URL, "https://app.example/"):  # static, PDF, browser escalation
        check(await client.post("/v1/fetch", json={"url": url}), 200, "Envelope_Document_")
    hit("POST", "/v1/fetch")

    # batch: one good URL, one failing; then the long-poll until done
    batch = check(
        await client.post("/v1/fetch/batch", json={"urls": [DOC_URL, "https://missing.example/x"]}),
        202,
        "Envelope_Job_",
    )
    hit("POST", "/v1/fetch/batch")
    job_id = batch["data"]["id"]
    check(await client.get(f"/v1/jobs/{job_id}"), 200, "Envelope_JobDetail_")
    done = check(
        await client.get(f"/v1/jobs/{job_id}", params={"wait": 30}), 200, "Envelope_JobDetail_"
    )
    assert done["data"]["job"]["status"] == "partial"  # one URL failed
    assert done["data"]["result"]["kind"] == "fetch_batch"
    assert done["data"]["result"]["failed"]
    hit("GET", "/v1/jobs/{job_id}")

    sr = check(
        await client.post(
            "/v1/search_read", json={"search": {"query": "python asyncio"}, "top_n": 5}
        ),
        202,
        "Envelope_Job_",
    )
    hit("POST", "/v1/search_read")
    sr_done = check(
        await client.get(f"/v1/jobs/{sr['data']['id']}", params={"wait": 30}),
        200,
        "Envelope_JobDetail_",
    )
    assert sr_done["data"]["job"]["status"] in {"done", "partial"}
    assert sr_done["data"]["result"]["kind"] == "search_read"
    assert sr_done["data"]["result"]["documents"]

    # DELETE of a finished job returns it unchanged
    gone = check(await client.delete(f"/v1/jobs/{job_id}"), 200, "Envelope_Job_")
    assert gone["data"]["status"] == "partial"
    hit("DELETE", "/v1/jobs/{job_id}")

    routes = await _covered_success_routes(client)
    assert routes - EXEMPT_SUCCESS - seen == set(), "a public route has no schema check here"


async def test_cancelled_job_validates(app: FastAPI, client: httpx.AsyncClient) -> None:
    import asyncio

    from research_engine.jobs.context import JobContext
    from research_engine_client.models import BatchFetchResult, JobType

    started = asyncio.Event()

    async def slow(ctx: JobContext) -> BatchFetchResult:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError

    app.state.services.jobs.register(JobType.FETCH_BATCH, slow)
    job = check(
        await client.post("/v1/fetch/batch", json={"urls": [DOC_URL]}), 202, "Envelope_Job_"
    )
    await asyncio.wait_for(started.wait(), 5)
    running = check(await client.get(f"/v1/jobs/{job['data']['id']}"), 200, "Envelope_JobDetail_")
    assert running["data"]["job"]["status"] == "running"
    cancelled = check(await client.delete(f"/v1/jobs/{job['data']['id']}"), 200, "Envelope_Job_")
    assert cancelled["data"]["status"] == "cancelled"
    detail = check(
        await client.get(f"/v1/jobs/{job['data']['id']}", params={"wait": 5}),
        200,
        "Envelope_JobDetail_",
    )
    assert detail["data"]["job"]["status"] == "cancelled"


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status", "code"),
    [
        ("GET", "/v1/jobs/" + "0" * 32, {}, 404, "not_found"),
        ("DELETE", "/v1/jobs/NOPE", {}, 404, "not_found"),
        ("GET", "/v1/jobs/abc123", {"params": {"wait": 61}}, 422, "invalid_request"),
        ("GET", "/v1/schemas/NoSuchModel", {}, 404, "not_found"),
        ("GET", "/v1/no-such-route", {}, 404, "not_found"),
        ("POST", "/v1/search", {"json": {}}, 422, "invalid_request"),
        ("POST", "/v1/search", {"content": b"{not json"}, 422, "invalid_request"),
        ("POST", "/v1/fetch", {"json": {"url": "ftp://x"}}, 422, "invalid_request"),
        ("POST", "/v1/fetch", {"json": {"url": f"https://{BLOCKED_HOST}/"}}, 403, "ssrf_blocked"),
        ("POST", "/v1/fetch", {"json": {"url": "https://missing.example/"}}, 502, "fetch_failed"),
        ("POST", "/v1/fetch/batch", {"json": {"urls": []}}, 422, "invalid_request"),
        ("POST", "/v1/search_read", {"json": {"search": {"query": ""}}}, 422, "invalid_request"),
        ("GET", "/v1/search", {}, 405, "invalid_request"),
    ],
)
async def test_error_responses_validate(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    status: int,
    code: str,
) -> None:
    body = check(await client.request(method, path, **kwargs), status, ERROR)
    assert body["data"] is None and body["errors"][0]["code"] == code


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/search"),
        ("GET", "/v1/engines"),
        ("GET", "/v1/jobs/abc"),
        ("GET", "/v1/schemas"),
    ],
)
@pytest.mark.parametrize("key", [None, "wrong-" + "k" * 32])
async def test_unauthorized_responses_validate(
    app: FastAPI, method: str, path: str, key: str | None
) -> None:
    headers = {} if key is None else {"X-API-Key": key}
    assert key != TEST_API_KEY
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost"
    ) as c:
        body = check(await c.request(method, path, headers=headers, json={}), 401, ERROR)
    assert body["errors"][0]["code"] == "unauthorized"


async def test_health_degraded_and_down_validate(app: FastAPI, client: httpx.AsyncClient) -> None:
    """/health degraded (200) and down (503) are still HealthReport envelopes."""

    async def down() -> bool:
        return False

    class BrokenEngine:
        def connect(self) -> None:
            raise OSError("gone")

    services = app.state.services
    services.health_checks["searxng"] = down
    services.health_ttl_s = 0
    body = check(await client.get("/health"), 200, "Envelope_HealthReport_")
    assert body["data"]["status"] == "degraded"
    real = services.engine
    services.engine = BrokenEngine()
    try:
        body = check(await client.get("/health"), 503, "Envelope_HealthReport_")
    finally:
        services.engine = real
    assert body["data"]["status"] == "down"
