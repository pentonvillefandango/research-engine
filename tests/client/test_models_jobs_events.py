from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from research_engine_client.models.events import Event, EventKind, EventLevel
from research_engine_client.models.health import (
    DependencyHealth,
    DependencyState,
    HealthReport,
)
from research_engine_client.models.jobs import (
    BatchFetchRequest,
    BatchFetchResult,
    Job,
    JobDetail,
    JobProgress,
    JobStatus,
    JobType,
    SearchReadRequest,
    SearchReadResult,
)
from research_engine_client.models.search import SearchRequest, SearchResponse

EXPECTED_KINDS = {
    "search.started",
    "search.engine_failed",
    "search.retry_broader",
    "search.done",
    "fetch.started",
    "fetch.static_done",
    "fetch.escalated",
    "fetch.browser_done",
    "fetch.failed",
    "fetch.done",
    "robots.disallowed",
    "robots.fetched",
    "cache.hit",
    "cache.miss",
    "job.queued",
    "job.started",
    "job.progress",
    "job.done",
    "job.partial",
    "job.failed",
    "job.cancelled",
    "system.startup",
    "system.shutdown",
    "system.health",
}


def test_job_status_values_and_terminal() -> None:
    assert {s.value for s in JobStatus} == {
        "queued",
        "running",
        "partial",
        "done",
        "failed",
        "cancelled",
    }
    assert {s for s in JobStatus if s.is_terminal} == {
        JobStatus.PARTIAL,
        JobStatus.DONE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }


def test_batch_limit() -> None:
    BatchFetchRequest(urls=tuple(f"https://a.example/{i}" for i in range(50)))
    with pytest.raises(ValidationError):
        BatchFetchRequest(urls=tuple(f"https://a.example/{i}" for i in range(51)))
    with pytest.raises(ValidationError):
        BatchFetchRequest(urls=())


def test_search_read_defaults() -> None:
    r = SearchReadRequest(search=SearchRequest(query="x"))
    assert r.top_n == 5 and r.session_id is None and r.fetch.mode == "auto"


def test_event_kinds_exact() -> None:
    assert {k.value for k in EventKind} == EXPECTED_KINDS


def test_event_defaults() -> None:
    e = Event(ts=datetime.now(UTC), level=EventLevel.INFO, kind=EventKind.JOB_DONE, message="ok")
    assert e.job_id is None and e.data == {} and e.id is None


def _job(status: JobStatus) -> Job:
    now = datetime.now(UTC)
    return Job(
        id="j1",
        type=JobType.SEARCH_READ,
        status=status,
        progress=JobProgress(done=0, total=0),
        request={},
        created_at=now,
    )


def test_job_detail_discriminated_union() -> None:
    d = JobDetail(
        job=_job(JobStatus.DONE),
        result=SearchReadResult(search=SearchResponse(query="x"), documents=[]),
    )
    again = JobDetail.model_validate_json(d.model_dump_json())
    assert isinstance(again.result, SearchReadResult)
    d2 = JobDetail(job=_job(JobStatus.DONE), result=BatchFetchResult(documents=[], failed=[]))
    assert isinstance(JobDetail.model_validate_json(d2.model_dump_json()).result, BatchFetchResult)


def test_health_report() -> None:
    h = HealthReport(
        status=DependencyState.UP,
        version="0.1.0",
        git_sha="abc",
        dependencies={"searxng": DependencyHealth(state=DependencyState.UP, latency_ms=3)},
    )
    assert h.dependencies["searxng"].detail is None
