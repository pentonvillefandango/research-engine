"""ResearchEngineClient driving the real app in-process (V1-13)."""

import httpx
import pytest
from research_engine_client import ResearchEngineClient, ResearchEngineError
from research_engine_client.models import (
    DependencyState,
    ErrorCode,
    FetchRequest,
    JobStatus,
    JobType,
    SearchReadRequest,
    SearchRequest,
)


@pytest.fixture
async def rc(app):
    async with ResearchEngineClient(
        "http://research.localhost", "test-key", transport=httpx.ASGITransport(app=app)
    ) as c:
        yield c


async def test_search(rc: ResearchEngineClient) -> None:
    resp = await rc.search(SearchRequest(query="python"))
    assert resp.query == "python" and resp.results
    assert rc.last_meta is not None and len(rc.last_meta.request_id) > 0


async def test_fetch(rc: ResearchEngineClient) -> None:
    doc = await rc.fetch(FetchRequest.model_validate({"url": "https://blog.example/post"}))
    assert doc.status == 200 and doc.markdown


async def test_search_read_and_wait_for_job(rc: ResearchEngineClient) -> None:
    req = SearchReadRequest.model_validate({"search": {"query": "python"}, "top_n": 1})
    job = await rc.search_read(req)
    assert job.type is JobType.SEARCH_READ
    detail = await rc.wait_for_job(job.id, timeout_s=30)
    assert detail.job.id == job.id and detail.job.status.is_terminal
    assert detail.job.status in {JobStatus.DONE, JobStatus.PARTIAL}
    assert detail.result is not None


async def test_ssrf_is_a_typed_error(rc: ResearchEngineClient) -> None:
    with pytest.raises(ResearchEngineError) as ei:
        await rc.fetch(FetchRequest.model_validate({"url": "https://fake-blocked.example/x"}))
    assert ei.value.status == 403 and not ei.value.retryable
    assert ei.value.errors[0].code is ErrorCode.SSRF_BLOCKED
    assert ei.value.request_id


async def test_wrong_key_is_unauthorized(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with ResearchEngineClient("http://research.localhost", "nope", transport=transport) as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.engines()
    assert ei.value.status == 401 and "nope" not in str(ei.value)


async def test_cancel_job(rc: ResearchEngineClient) -> None:
    req = SearchReadRequest.model_validate({"search": {"query": "python"}, "top_n": 1})
    job = await rc.search_read(req)
    cancelled = await rc.cancel_job(job.id)
    assert cancelled.id == job.id
    assert (await rc.get_job(job.id)).job.status.is_terminal


async def test_engines_health_version(rc: ResearchEngineClient) -> None:
    assert (await rc.engines()).engines is not None
    report = await rc.health()
    assert report.status is DependencyState.UP and "database" in report.dependencies
    info = await rc.version()
    assert info.version and info.schema_version == "1.0.0"


async def test_health_returns_report_when_database_down(app, rc: ResearchEngineClient) -> None:
    class BrokenEngine:
        def connect(self):
            raise OSError("gone")

    real = app.state.services.engine
    app.state.services.engine = BrokenEngine()
    try:
        report = await rc.health()
    finally:
        app.state.services.engine = real
    assert report.status is DependencyState.DOWN
    assert report.dependencies["database"].state is DependencyState.DOWN
