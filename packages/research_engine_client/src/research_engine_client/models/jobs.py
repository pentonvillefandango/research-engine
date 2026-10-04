"""Job models and job request/result types (V1-07..V1-09)."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field

from ._base import Model, RequestModel
from .common import ErrorDetail
from .document import Document, DocumentFormat, FetchMode, HttpUrlStr
from .search import SearchRequest, SearchResponse


class JobType(StrEnum):
    FETCH_BATCH = "fetch_batch"
    SEARCH_READ = "search_read"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PARTIAL = "partial"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.PARTIAL, JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


class JobProgress(Model):
    done: int = Field(ge=0)
    total: int = Field(ge=0)
    current: str | None = Field(default=None, description="URL or engine currently in use")


class Job(Model):
    id: str
    type: JobType
    status: JobStatus
    progress: JobProgress
    parent_id: str | None = None
    session_id: str | None = None
    request: dict[str, Any]
    result_ref: str | None = None
    errors: list[ErrorDetail] = Field(default_factory=list[ErrorDetail])
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class FetchRequestOptions(RequestModel):
    mode: FetchMode = FetchMode.AUTO
    formats: tuple[DocumentFormat, ...] = (DocumentFormat.MARKDOWN,)
    use_cache: bool = True
    timeout_s: int = Field(default=60, ge=1, le=300)


class BatchFetchRequest(RequestModel):
    urls: tuple[HttpUrlStr, ...] = Field(min_length=1, max_length=50)
    mode: FetchMode = FetchMode.AUTO
    formats: tuple[DocumentFormat, ...] = (DocumentFormat.MARKDOWN,)
    use_cache: bool = True
    timeout_s: int = Field(default=60, ge=1, le=300)
    session_id: str | None = None


class SearchReadRequest(RequestModel):
    search: SearchRequest
    top_n: int = Field(default=5, ge=1, le=20)
    fetch: FetchRequestOptions = FetchRequestOptions()
    session_id: str | None = None


class FailedUrl(Model):
    url: str
    error: ErrorDetail


class BatchFetchResult(Model):
    kind: Literal["fetch_batch"] = "fetch_batch"
    documents: list[Document]
    failed: list[FailedUrl]


class RankedDocument(Model):
    search_rank: int = Field(ge=1)
    search_score: float = Field(ge=0)
    document: Document


class SearchReadResult(Model):
    kind: Literal["search_read"] = "search_read"
    search: SearchResponse
    documents: list[RankedDocument]
    failed: list[FailedUrl] = Field(default_factory=list[FailedUrl])


JobResult = Annotated[BatchFetchResult | SearchReadResult, Field(discriminator="kind")]


class JobDetail(Model):
    job: Job
    result: JobResult | None = None
