# Step 1: Models package and JSON Schema export

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** §4 (structured data contracts), V1-14 (schemas feed OpenAPI).

**Outcome:**
- `research_engine_client.models` holds every V1 model.
- `ALL_MODELS` is the schema registry.
- `research-engine schemas export` writes `schemas/*.json`.
- A test fails if `schemas/` is stale.

**Rules the tests enforce:**
- Optional fields are `X | None = None` and **appear in the serialised output** (as `null`).
- Enums are used wherever the set of values is known.
- Every model uses `model_config = ConfigDict(extra="forbid")`. Request models are frozen.
- `datetime` values are timezone-aware UTC.

---

### Task 1.1: Common models: Envelope, Meta, ErrorDetail, ErrorCode

**Goal:** Implement the generic response envelope and typed errors that every endpoint returns.

**Files:**
- Create: `packages/research_engine_client/src/research_engine_client/models/__init__.py` (temporary re-exports; finalised in 1.4)
- Create: `packages/research_engine_client/src/research_engine_client/models/_base.py`
- Create: `packages/research_engine_client/src/research_engine_client/models/common.py`
- Test: `tests/client/test_models_common.py`

**Acceptance Criteria:**
- [ ] `Envelope[SearchResponse]`-style parametrisation works, and `Envelope[int](data=1, meta=...)` round-trips through JSON.
- [ ] `Envelope.model_dump(mode="json")` always includes the `data`, `meta` and `errors` keys, even when `data` is `None`.
- [ ] `ErrorDetail(code="bogus", ...)` raises `ValidationError`.
- [ ] `Meta.schema_version` defaults to `SCHEMA_VERSION == "1.0.0"`.

**Verify:** `uv run pytest tests/client/test_models_common.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/client/test_models_common.py
import pytest
from pydantic import ValidationError

from research_engine_client.models.common import (
    SCHEMA_VERSION, Envelope, ErrorCode, ErrorDetail, Meta,
)


def test_schema_version_is_semver() -> None:
    assert SCHEMA_VERSION == "1.0.0"


def test_meta_defaults() -> None:
    meta = Meta(request_id="r1", took_ms=5)
    assert meta.schema_version == SCHEMA_VERSION
    assert meta.cache_hit is False


def test_envelope_roundtrip_generic() -> None:
    env = Envelope[int](data=1, meta=Meta(request_id="r1", took_ms=1))
    again = Envelope[int].model_validate_json(env.model_dump_json())
    assert again == env


def test_envelope_always_serialises_all_keys() -> None:
    env = Envelope[int](data=None, meta=Meta(request_id="r", took_ms=0),
                        errors=[ErrorDetail(code=ErrorCode.NOT_FOUND, message="x", retryable=False)])
    dumped = env.model_dump(mode="json")
    assert set(dumped) == {"data", "meta", "errors"}
    assert dumped["data"] is None
    assert dumped["errors"][0] == {"code": "not_found", "message": "x", "retryable": False, "source": None}


def test_error_code_is_enum() -> None:
    with pytest.raises(ValidationError):
        ErrorDetail(code="bogus", message="x", retryable=False)  # type: ignore[arg-type]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Meta(request_id="r", took_ms=1, surprise=True)  # type: ignore[call-arg]
```

- [ ] **Step 2: Run them.** `uv run pytest tests/client/test_models_common.py -v`. Expected: FAIL with `ModuleNotFoundError: research_engine_client.models`.

- [ ] **Step 3: Implement `_base.py` and `common.py`**

```python
# models/_base.py
"""Base classes shared by every public model."""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """Response/data model: strict about unknown fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RequestModel(BaseModel):
    """Request model: frozen so it can be hashed into cache keys."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
```

```python
# models/common.py
"""Envelope, metadata and typed errors shared by every response."""

from enum import StrEnum

from pydantic import Field

from ._base import Model

SCHEMA_VERSION = "1.0.0"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    SSRF_BLOCKED = "ssrf_blocked"
    ROBOTS_DISALLOWED = "robots_disallowed"
    CONTENT_TYPE_NOT_ALLOWED = "content_type_not_allowed"
    RESPONSE_TOO_LARGE = "response_too_large"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_ERROR = "upstream_error"
    ENGINE_FAILED = "engine_failed"
    FETCH_FAILED = "fetch_failed"
    EXTRACTION_FAILED = "extraction_failed"
    JOB_CANCELLED = "job_cancelled"
    JOB_TIMEOUT = "job_timeout"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class ErrorDetail(Model):
    code: ErrorCode
    message: str
    retryable: bool
    source: str | None = Field(default=None, description="URL, engine or component that failed")


class Meta(Model):
    request_id: str
    schema_version: str = SCHEMA_VERSION
    took_ms: int = Field(ge=0)
    cache_hit: bool = False


class Envelope[T](Model):
    data: T | None = None
    meta: Meta
    errors: list[ErrorDetail] = Field(default_factory=list)
```

Generic syntax note: Pydantic 2.13 supports PEP 695 `class Envelope[T](BaseModel)`. If pyright strict complains, fall back to `Generic[T]` with a `TypeVar`.

`models/__init__.py` re-exports these names for now.

- [ ] **Step 4: Run the tests.** Expected: PASS (6 passed).

- [ ] **Step 5: Commit.** `git commit -m "feat(models): envelope, meta and typed errors (§4)"`

---

### Task 1.2: Search and document models

**Goal:** Implement the search and fetch request/response models exactly as §4 lists them.

**Files:**
- Create: `models/search.py`, `models/document.py` (under `packages/research_engine_client/src/research_engine_client/`)
- Test: `tests/client/test_models_search.py`, `tests/client/test_models_document.py`

**Acceptance Criteria:**
- [ ] `SearchRequest(query="x")` has these defaults: `intent=general`, `max_results=20`, `language="en-GB"`, `depth=standard`, `time_range=None`, `engines=None`, `use_cache=True`.
- [ ] `max_results` is constrained to 1..100. An empty or whitespace-only `query` is rejected.
- [ ] `FetchRequest.url` accepts only http/https. `timeout_s` is in 1..300 and defaults to 60. `formats` defaults to `[markdown]`.
- [ ] `Document` contains every §4 field. `Provenance.method` is a `FetchMethod` enum and `content_hash` matches `^sha256:[0-9a-f]{64}$`.
- [ ] `SearchRequest` is hashable (frozen).

**Verify:** `uv run pytest tests/client -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/client/test_models_search.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_engine_client.models.search import (
    SearchDepth, SearchIntent, SearchRequest, SearchResponse, SearchResult,
    TimeRange, UnresponsiveEngine,
)


def test_search_request_defaults() -> None:
    r = SearchRequest(query="vector databases")
    assert (r.intent, r.max_results, r.language, r.depth) == (
        SearchIntent.GENERAL, 20, "en-GB", SearchDepth.STANDARD)
    assert r.time_range is None and r.engines is None and r.use_cache is True


@pytest.mark.parametrize("bad", ["", "   "])
def test_search_request_rejects_blank_query(bad: str) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query=bad)


@pytest.mark.parametrize("n", [0, 101])
def test_max_results_bounds(n: int) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="x", max_results=n)


def test_search_request_hashable() -> None:
    assert hash(SearchRequest(query="x", time_range=TimeRange.WEEK))


def test_intents_cover_requirements() -> None:
    assert {i.value for i in SearchIntent} == {
        "general", "technical", "library", "product", "standard", "news", "academic"}


def test_search_response_serialises_nulls() -> None:
    res = SearchResult(rank=1, url="https://a.example/x", canonical_url="https://a.example/x",
                       title="t", snippet="s", domain="a.example", engines=["brave"], score=0.5)
    resp = SearchResponse(query="q", results=[res],
                          unresponsive_engines=[UnresponsiveEngine(engine="google", error="timeout")])
    dumped = resp.model_dump(mode="json")
    assert dumped["results"][0]["published_at"] is None
    assert dumped["suggestions"] == [] and dumped["infoboxes"] == []


def test_published_at_is_aware() -> None:
    res = SearchResult(rank=1, url="https://a.example", canonical_url="https://a.example",
                       title="t", snippet="", domain="a.example", engines=[], score=0,
                       published_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert res.published_at is not None and res.published_at.tzinfo is not None
```

```python
# tests/client/test_models_document.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_engine_client.models.document import (
    Document, DocumentFormat, FetchMethod, FetchMode, FetchRequest, Link,
    Provenance, Quality, StructuredData, Table,
)

HASH = "sha256:" + "a" * 64


def _doc(**kw: object) -> Document:
    base: dict[str, object] = dict(
        url="https://a.example/p", final_url="https://a.example/p", status=200, title="T",
        language="en", markdown="# T", word_count=1,
        provenance=Provenance(url="https://a.example/p", fetched_at=datetime.now(UTC),
                              content_hash=HASH, method=FetchMethod.STATIC),
        quality=Quality(word_count=1, text_html_ratio=0.5, has_title=True))
    base.update(kw)
    return Document.model_validate(base)


def test_fetch_request_defaults() -> None:
    r = FetchRequest(url="https://a.example")
    assert r.mode is FetchMode.AUTO and r.formats == (DocumentFormat.MARKDOWN,)
    assert r.timeout_s == 60 and r.use_cache is True


@pytest.mark.parametrize("url", ["ftp://a.example", "file:///etc/passwd", "javascript:alert(1)"])
def test_fetch_request_rejects_non_http(url: str) -> None:
    with pytest.raises(ValidationError):
        FetchRequest(url=url)


@pytest.mark.parametrize("t", [0, 301])
def test_timeout_bounds(t: int) -> None:
    with pytest.raises(ValidationError):
        FetchRequest(url="https://a.example", timeout_s=t)


def test_document_defaults_and_nulls() -> None:
    d = _doc().model_dump(mode="json")
    for key in ("author", "published_at", "html"):
        assert key in d and d[key] is None
    assert d["links"] == [] and d["tables"] == [] and d["warnings"] == []
    assert d["structured_data"] == {"json_ld": [], "microdata": [], "opengraph": {}}
    assert d["provenance"]["job_id"] is None


def test_content_hash_format_enforced() -> None:
    with pytest.raises(ValidationError):
        Provenance(url="https://a.example", fetched_at=datetime.now(UTC),
                   content_hash="md5:abc", method=FetchMethod.STATIC)


def test_table_shape() -> None:
    t = Table(headers=["a", "b"], rows=[["1", "2"]])
    assert t.caption is None and t.source_selector is None


def test_link_model() -> None:
    assert Link(url="https://b.example", text="B", external=True).external


def test_structured_data_defaults() -> None:
    assert StructuredData().json_ld == []
```

- [ ] **Step 2: Run them.** Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `models/search.py`**

```python
"""Search request/response models (V1-01..V1-03)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, StringConstraints

from ._base import Model, RequestModel

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class SearchIntent(StrEnum):
    GENERAL = "general"
    TECHNICAL = "technical"
    LIBRARY = "library"
    PRODUCT = "product"
    STANDARD = "standard"
    NEWS = "news"
    ACADEMIC = "academic"


class SearchDepth(StrEnum):
    """How many SearXNG result pages to request: quick=1, standard=2, deep=3."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class TimeRange(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class SearchRequest(RequestModel):
    query: NonBlank
    intent: SearchIntent = SearchIntent.GENERAL
    max_results: int = Field(default=20, ge=1, le=100)
    time_range: TimeRange | None = None
    language: str = Field(default="en-GB", pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$|^all$")
    engines: tuple[str, ...] | None = Field(default=None, description="Override the intent preset's engines")
    depth: SearchDepth = SearchDepth.STANDARD
    use_cache: bool = True


class SearchResult(Model):
    rank: int = Field(ge=1)
    url: str
    canonical_url: str
    title: str
    snippet: str
    domain: str
    engines: list[str]
    score: float = Field(ge=0)
    published_at: AwareDatetime | None = None


class InfoboxLink(Model):
    title: str
    url: str


class Infobox(Model):
    title: str
    content: str | None = None
    engine: str | None = None
    urls: list[InfoboxLink] = Field(default_factory=list)


class UnresponsiveEngine(Model):
    engine: str
    error: str


class SearchResponse(Model):
    query: str
    results: list[SearchResult] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    infoboxes: list[Infobox] = Field(default_factory=list)
    unresponsive_engines: list[UnresponsiveEngine] = Field(default_factory=list)


class IntentPreset(Model):
    categories: list[str]
    engines: list[str]
    description: str


class EngineInfo(Model):
    name: str
    used_by: list[SearchIntent]


class EnginesResponse(Model):
    engines: list[EngineInfo]
    intents: dict[SearchIntent, IntentPreset]


__all__ = [
    "EngineInfo", "EnginesResponse", "Infobox", "InfoboxLink", "IntentPreset",
    "SearchDepth", "SearchIntent", "SearchRequest", "SearchResponse", "SearchResult",
    "TimeRange", "UnresponsiveEngine", "datetime",
]
```

Remove `"datetime"` from `__all__` if ruff flags it; the import exists only for the `AwareDatetime` docs. Prefer deleting the unused import.

- [ ] **Step 4: Implement `models/document.py`**

```python
"""Fetch request and Document models (V1-04..V1-06)."""

from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, AwareDatetime, Field

from ._base import Model, RequestModel


def _http_url(value: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    return value


HttpUrlStr = Annotated[str, Field(max_length=4096), AfterValidator(_http_url)]


class FetchMode(StrEnum):
    AUTO = "auto"
    STATIC = "static"
    BROWSER = "browser"


class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    HTML = "html"


class FetchMethod(StrEnum):
    STATIC = "static"
    BROWSER = "browser"
    API = "api"
    ARCHIVE = "archive"


class EscalationReason(StrEnum):
    STATIC_FAILED = "static_failed"
    THIN_CONTENT = "thin_content"
    JS_RENDERED = "js_rendered"
    FORCED = "forced"


class FetchRequest(RequestModel):
    url: HttpUrlStr
    mode: FetchMode = FetchMode.AUTO
    formats: tuple[DocumentFormat, ...] = (DocumentFormat.MARKDOWN,)
    use_cache: bool = True
    timeout_s: int = Field(default=60, ge=1, le=300)


class Link(Model):
    url: str
    text: str
    external: bool


class Table(Model):
    caption: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    source_selector: str | None = None


class StructuredData(Model):
    json_ld: list[dict[str, Any]] = Field(default_factory=list)
    microdata: list[dict[str, Any]] = Field(default_factory=list)
    opengraph: dict[str, Any] = Field(default_factory=dict)


class Provenance(Model):
    url: str
    fetched_at: AwareDatetime
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    method: FetchMethod
    job_id: str | None = None


class Quality(Model):
    word_count: int = Field(ge=0)
    text_html_ratio: float = Field(ge=0, le=1)
    has_title: bool
    escalation_reason: EscalationReason | None = None


class Document(Model):
    url: str
    final_url: str
    status: int
    title: str | None = None
    author: str | None = None
    published_at: AwareDatetime | None = None
    language: str | None = None
    markdown: str
    html: str | None = Field(default=None, description="Raw HTML, only when 'html' is in formats")
    word_count: int = Field(ge=0)
    links: list[Link] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    structured_data: StructuredData = Field(default_factory=StructuredData)
    provenance: Provenance
    quality: Quality
    warnings: list[str] = Field(default_factory=list)
```

Note: `title` and `language` are nullable. Real pages sometimes lack them, and §4 lists them without a `?`, but the "never silently omit" rule is satisfied by an explicit `null`. The test `_doc` passes values for both.

- [ ] **Step 5: Run the tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git commit -m "feat(models): search and document models (§4, V1-01, V1-04)"`

---

### Task 1.3: Job, event and health models

**Goal:** Implement the job, event and health models, including the job request and result types used in step 5.

**Files:**
- Create: `models/jobs.py`, `models/events.py`, `models/health.py`
- Test: `tests/client/test_models_jobs_events.py`

**Acceptance Criteria:**
- [ ] `JobStatus` is exactly `{queued, running, partial, done, failed, cancelled}`. `JobStatus.is_terminal` is true for `partial`, `done`, `failed` and `cancelled`.
- [ ] `BatchFetchRequest.urls` takes 1..50 http(s) URLs. A 51st URL makes it fail validation.
- [ ] `SearchReadRequest` has `search: SearchRequest`, `top_n` (1..20, default 5), `fetch: FetchRequestOptions`, and `session_id: str | None`.
- [ ] `EventKind` values match the index file's list exactly.
- [ ] `JobDetail.result` is a discriminated union over `BatchFetchResult | SearchReadResult | None`, keyed by `kind`.

**Verify:** `uv run pytest tests/client -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/client/test_models_jobs_events.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_engine_client.models.events import Event, EventKind, EventLevel
from research_engine_client.models.health import DependencyHealth, DependencyState, HealthReport
from research_engine_client.models.jobs import (
    BatchFetchRequest, BatchFetchResult, Job, JobDetail, JobProgress, JobStatus, JobType,
    SearchReadRequest, SearchReadResult,
)
from research_engine_client.models.search import SearchRequest, SearchResponse

EXPECTED_KINDS = {
    "search.started", "search.engine_failed", "search.retry_broader", "search.done",
    "fetch.started", "fetch.static_done", "fetch.escalated", "fetch.browser_done",
    "fetch.failed", "fetch.done", "robots.disallowed", "robots.fetched", "cache.hit",
    "cache.miss", "job.queued", "job.started", "job.progress", "job.done", "job.partial",
    "job.failed", "job.cancelled", "system.startup", "system.shutdown", "system.health",
}


def test_job_status_values_and_terminal() -> None:
    assert {s.value for s in JobStatus} == {"queued", "running", "partial", "done", "failed", "cancelled"}
    assert {s for s in JobStatus if s.is_terminal} == {
        JobStatus.PARTIAL, JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


def test_batch_limit() -> None:
    BatchFetchRequest(urls=[f"https://a.example/{i}" for i in range(50)])
    with pytest.raises(ValidationError):
        BatchFetchRequest(urls=[f"https://a.example/{i}" for i in range(51)])
    with pytest.raises(ValidationError):
        BatchFetchRequest(urls=[])


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
    return Job(id="j1", type=JobType.SEARCH_READ, status=status, progress=JobProgress(done=0, total=0),
               request={}, created_at=now)


def test_job_detail_discriminated_union() -> None:
    d = JobDetail(job=_job(JobStatus.DONE),
                  result=SearchReadResult(search=SearchResponse(query="x"), documents=[]))
    again = JobDetail.model_validate_json(d.model_dump_json())
    assert isinstance(again.result, SearchReadResult)
    d2 = JobDetail(job=_job(JobStatus.DONE), result=BatchFetchResult(documents=[], failed=[]))
    assert isinstance(JobDetail.model_validate_json(d2.model_dump_json()).result, BatchFetchResult)


def test_health_report() -> None:
    h = HealthReport(status=DependencyState.UP, version="0.1.0", git_sha="abc",
                     dependencies={"searxng": DependencyHealth(state=DependencyState.UP, latency_ms=3)})
    assert h.dependencies["searxng"].detail is None
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `models/events.py`**

```python
"""Event model (V1-15)."""

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from ._base import Model


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventKind(StrEnum):
    SEARCH_STARTED = "search.started"
    SEARCH_ENGINE_FAILED = "search.engine_failed"
    SEARCH_RETRY_BROADER = "search.retry_broader"
    SEARCH_DONE = "search.done"
    FETCH_STARTED = "fetch.started"
    FETCH_STATIC_DONE = "fetch.static_done"
    FETCH_ESCALATED = "fetch.escalated"
    FETCH_BROWSER_DONE = "fetch.browser_done"
    FETCH_FAILED = "fetch.failed"
    FETCH_DONE = "fetch.done"
    ROBOTS_DISALLOWED = "robots.disallowed"
    ROBOTS_FETCHED = "robots.fetched"
    CACHE_HIT = "cache.hit"
    CACHE_MISS = "cache.miss"
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_PROGRESS = "job.progress"
    JOB_DONE = "job.done"
    JOB_PARTIAL = "job.partial"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_HEALTH = "system.health"


class Event(Model):
    id: int | None = Field(default=None, description="Store-assigned sequence number")
    ts: AwareDatetime
    job_id: str | None = None
    level: EventLevel
    kind: EventKind
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Implement `models/jobs.py`**

```python
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
    errors: list[ErrorDetail] = Field(default_factory=list)
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
    failed: list[FailedUrl] = Field(default_factory=list)


JobResult = Annotated[BatchFetchResult | SearchReadResult, Field(discriminator="kind")]


class JobDetail(Model):
    job: Job
    result: JobResult | None = None
```

- [ ] **Step 5: Implement `models/health.py`**

```python
"""Health and version models (V1-18, §10)."""

from enum import StrEnum

from pydantic import Field

from ._base import Model


class DependencyState(StrEnum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"


class DependencyHealth(Model):
    state: DependencyState
    latency_ms: int | None = None
    detail: str | None = None


class VersionInfo(Model):
    version: str
    git_sha: str
    schema_version: str


class HealthReport(Model):
    status: DependencyState
    version: str
    git_sha: str
    dependencies: dict[str, DependencyHealth] = Field(default_factory=dict)
```

- [ ] **Step 6: Run the tests.** Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(models): job, event and health models (§4, V1-09, V1-15)"`

---

### Task 1.4: Schema registry, export CLI and staleness test

**Goal:** Build `ALL_MODELS`, a `research-engine schemas export [--check]` command, the committed `schemas/*.json`, and a test that every model's JSON Schema matches the committed file and validates sample instances.

**Files:**
- Modify: `packages/research_engine_client/src/research_engine_client/models/__init__.py`
- Create: `packages/research_engine_client/src/research_engine_client/schemas.py`
- Create: `packages/research_engine/src/research_engine/cli.py`
- Modify: `packages/research_engine/pyproject.toml` (add `[project.scripts]`)
- Create: `schemas/*.json` (generated)
- Test: `tests/client/test_schemas.py`

**Acceptance Criteria:**
- [ ] `ALL_MODELS` keys are the model names (for example `"Document"`, `"Envelope_SearchResponse_"`). It includes the concrete envelopes for each endpoint: `Envelope[SearchResponse]`, `Envelope[Document]`, `Envelope[Job]`, `Envelope[JobDetail]`, `Envelope[EnginesResponse]` and `Envelope[HealthReport]`.
- [ ] `uv run research-engine schemas export` writes one `schemas/<Name>.json` per model, with sorted keys, 2-space indent and a trailing newline.
- [ ] `uv run research-engine schemas export --check` exits 1 and lists the stale files if any differ, and exits 0 otherwise.
- [ ] The test validates a sample of each core model's `model_dump(mode="json")` against its exported schema with `jsonschema` (Draft 2020-12).

**Verify:** `uv run research-engine schemas export --check && uv run pytest tests/client/test_schemas.py -v` → `schemas up to date`, then all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/client/test_schemas.py
import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from research_engine_client.models import ALL_MODELS, Envelope, Meta, SearchResponse
from research_engine_client.models.document import Document, FetchMethod, Provenance, Quality
from research_engine_client.schemas import render_schemas

ROOT = Path(__file__).resolve().parents[2]


def test_registry_contains_core_models() -> None:
    for name in ("Document", "SearchRequest", "SearchResponse", "Job", "JobDetail", "Event",
                 "ErrorDetail", "Envelope_SearchResponse_", "Envelope_Document_"):
        assert name in ALL_MODELS, name


def test_committed_schemas_are_current() -> None:
    rendered = render_schemas()
    for name, text in rendered.items():
        path = ROOT / "schemas" / f"{name}.json"
        assert path.exists(), f"missing {path}; run: uv run research-engine schemas export"
        assert path.read_text() == text, f"stale {path}; run: uv run research-engine schemas export"


def _sample_document() -> Document:
    return Document(url="https://a.example", final_url="https://a.example", status=200, title="t",
                    language="en", markdown="x", word_count=1,
                    provenance=Provenance(url="https://a.example", fetched_at=datetime.now(UTC),
                                          content_hash="sha256:" + "0" * 64, method=FetchMethod.STATIC),
                    quality=Quality(word_count=1, text_html_ratio=0.1, has_title=True))


@pytest.mark.parametrize(("name", "instance"), [
    ("Document", _sample_document()),
    ("Envelope_SearchResponse_", Envelope[SearchResponse](data=SearchResponse(query="q"),
                                                          meta=Meta(request_id="r", took_ms=1))),
])
def test_instances_validate_against_schema(name: str, instance: object) -> None:
    schema = json.loads(render_schemas()[name])
    jsonschema.Draft202012Validator(schema).validate(instance.model_dump(mode="json"))  # type: ignore[attr-defined]
```

- [ ] **Step 2: Run them.** Expected: FAIL (`ImportError: ALL_MODELS`).

- [ ] **Step 3: Implement the registry in `models/__init__.py`**

```python
"""Public, versioned data contracts for the Research Engine (REQUIREMENTS §4)."""

from pydantic import BaseModel

from .common import SCHEMA_VERSION, Envelope, ErrorCode, ErrorDetail, Meta
from .document import (
    Document, DocumentFormat, EscalationReason, FetchMethod, FetchMode, FetchRequest, Link,
    Provenance, Quality, StructuredData, Table,
)
from .events import Event, EventKind, EventLevel
from .health import DependencyHealth, DependencyState, HealthReport, VersionInfo
from .jobs import (
    BatchFetchRequest, BatchFetchResult, FailedUrl, FetchRequestOptions, Job, JobDetail,
    JobProgress, JobStatus, JobType, RankedDocument, SearchReadRequest, SearchReadResult,
)
from .search import (
    EngineInfo, EnginesResponse, Infobox, InfoboxLink, IntentPreset, SearchDepth, SearchIntent,
    SearchRequest, SearchResponse, SearchResult, TimeRange, UnresponsiveEngine,
)

_PLAIN: list[type[BaseModel]] = [
    ErrorDetail, Meta, SearchRequest, SearchResult, SearchResponse, Infobox, UnresponsiveEngine,
    EnginesResponse, FetchRequest, Document, Table, StructuredData, Provenance, Quality, Link,
    Job, JobDetail, BatchFetchRequest, SearchReadRequest, BatchFetchResult, SearchReadResult,
    Event, HealthReport, VersionInfo,
]
_ENVELOPED: list[type[BaseModel]] = [
    SearchResponse, Document, Job, JobDetail, EnginesResponse, HealthReport, VersionInfo,
]


def _envelope_name(inner: type[BaseModel]) -> str:
    return f"Envelope_{inner.__name__}_"


ALL_MODELS: dict[str, type[BaseModel]] = {m.__name__: m for m in _PLAIN} | {
    _envelope_name(m): Envelope[m] for m in _ENVELOPED  # type: ignore[valid-type]
}

__all__ = [
    "ALL_MODELS", "SCHEMA_VERSION", "BatchFetchRequest", "BatchFetchResult", "DependencyHealth",
    "DependencyState", "Document", "DocumentFormat", "EngineInfo", "EnginesResponse", "Envelope",
    "ErrorCode", "ErrorDetail", "EscalationReason", "Event", "EventKind", "EventLevel",
    "FailedUrl", "FetchMethod", "FetchMode", "FetchRequest", "FetchRequestOptions",
    "HealthReport", "Infobox", "InfoboxLink", "IntentPreset", "Job", "JobDetail", "JobProgress",
    "JobStatus", "JobType", "Link", "Meta", "Provenance", "Quality", "RankedDocument",
    "SearchDepth", "SearchIntent", "SearchReadRequest", "SearchReadResult", "SearchRequest",
    "SearchResponse", "SearchResult", "StructuredData", "Table", "TimeRange",
    "UnresponsiveEngine", "VersionInfo",
]
```

- [ ] **Step 4: Implement `research_engine_client/schemas.py`**

```python
"""Render JSON Schemas for every public model."""

import json

from .models import ALL_MODELS, SCHEMA_VERSION


def render_schemas() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, model in sorted(ALL_MODELS.items()):
        schema = model.model_json_schema(mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"research-engine/{SCHEMA_VERSION}/{name}.json"
        schema["title"] = name
        out[name] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out
```

- [ ] **Step 5: Implement `research_engine/cli.py`** with argparse; no new dependency.

```python
"""`research-engine` command line: schema export now; more subcommands later."""

import argparse
import sys
from pathlib import Path

from research_engine_client.schemas import render_schemas


def _export(out_dir: Path, check: bool) -> int:
    rendered = render_schemas()
    stale = [n for n, t in rendered.items()
             if not (out_dir / f"{n}.json").exists() or (out_dir / f"{n}.json").read_text() != t]
    extra = sorted(p.stem for p in out_dir.glob("*.json") if p.stem not in rendered) if out_dir.exists() else []
    if check:
        if stale or extra:
            print("stale schemas:", ", ".join(sorted(stale + extra)), file=sys.stderr)
            return 1
        print("schemas up to date")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in extra:
        (out_dir / f"{name}.json").unlink()
    for name, text in rendered.items():
        (out_dir / f"{name}.json").write_text(text)
    print(f"wrote {len(rendered)} schemas to {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    schemas = sub.add_parser("schemas", help="JSON Schema tools")
    schemas_sub = schemas.add_subparsers(dest="action", required=True)
    export = schemas_sub.add_parser("export", help="write schemas/*.json")
    export.add_argument("--out", type=Path, default=Path("schemas"))
    export.add_argument("--check", action="store_true", help="fail if committed schemas are stale")
    args = parser.parse_args(argv)
    if args.cmd == "schemas" and args.action == "export":
        return _export(args.out, args.check)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

Add `[project.scripts] research-engine = "research_engine.cli:main"` to `packages/research_engine/pyproject.toml`, then run `uv sync`.

- [ ] **Step 6: Generate the schemas.** Run `uv run research-engine schemas export`. Expected: `wrote N schemas to schemas`.

- [ ] **Step 7: Run Verify.** Expected: PASS.

- [ ] **Step 8: Add the CI check.** In `.github/workflows/ci.yml` `lint-test`, after pyright, add `- run: uv run research-engine schemas export --check`.

- [ ] **Step 9: Run the full step check** (Global Constraint 11).

- [ ] **Step 10: Commit.** `git commit -m "feat(models): schema registry, export CLI and committed JSON Schemas (§4, V1-14)"`

---

**End of step 1:** request code review, report to the owner with evidence, and **stop** for approval. After approval, run `git push`.
