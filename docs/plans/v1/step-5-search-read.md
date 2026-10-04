# Step 5: `/v1/search_read`, batch fetch, health and version

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** V1-07 (batch fetch), V1-08 (search and read), V1-14 (OpenAPI examples), V1-18 (health), §10 (`/version`).

**Outcome:**
- The two job-backed endpoints work end to end on the real runner.
- `/health` reports each dependency's state, plus the version and git SHA. `/version` returns that identity on its own.
- A test enforces that every public endpoint has an OpenAPI example and an `Envelope` response schema.

**Task order:** 5.1 runs first. 5.2 follows it, because both modify `api/jobs_submit.py` and `app.py` and so are serialised. 5.3 runs last.

---

### Task 5.1: Batch fetch job and `POST /v1/fetch/batch`

**Goal:** Fetch up to 50 URLs as a job with bounded concurrency, per-URL error isolation and progress.

**Files:**
- Create: `packages/research_engine/src/research_engine/pipeline/search_read.py` (contains `run_batch_fetch`; `run_search_read` is added in 5.2)
- Create: `packages/research_engine/src/research_engine/api/jobs_submit.py`
- Modify: `packages/research_engine/src/research_engine/app.py` (register the handler, include the router)
- Test: `tests/service/unit/test_batch_fetch.py`

**Acceptance Criteria:**
- [ ] `POST /v1/fetch/batch` returns **202** with `Envelope[Job]` (`status=queued`) at once, and a `Location: /v1/jobs/{id}` header.
- [ ] It fetches with at most `job_fetch_concurrency` URLs in flight. A test uses a fake `FetchService` that records peak concurrency.
- [ ] Documents come back in **input order**. Each failure becomes `FailedUrl(url, error)` plus a `ctx.add_error`, and the job ends `partial`.
- [ ] If **every** URL fails, the job ends `failed` with code `fetch_failed` and the message `"all N URLs failed"`. The per-URL errors are kept in `job.errors`.
- [ ] Progress is `{done: k, total: N, current: <url>}`, updated after each URL. Every fetch passes `job_id`, so its events show in the job timeline.
- [ ] Duplicate URLs, by canonical form, are fetched once. Each input position still gets its document.
- [ ] More than 50 URLs gives 422 with `invalid_request`, coming from the model.

**Verify:** `uv run pytest tests/service/unit/test_batch_fetch.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_batch_fetch.py
import asyncio
from datetime import UTC, datetime

import httpx

from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.events.memory import InMemoryEventBus
from research_engine.jobs.context import JobContext
from research_engine.pipeline.search_read import run_batch_fetch
from research_engine_client.models import (
    BatchFetchRequest, Document, ErrorCode, FetchMethod, FetchRequest, Provenance, Quality,
)


def doc(url: str) -> Document:
    return Document(url=url, final_url=url, status=200, title="t", language="en", markdown="m", word_count=1,
                    provenance=Provenance(url=url, fetched_at=datetime.now(UTC), content_hash="sha256:" + "0" * 64,
                                          method=FetchMethod.STATIC),
                    quality=Quality(word_count=1, text_html_ratio=0.1, has_title=True))


class FakeFetch:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.inflight = 0
        self.peak = 0
        self.calls: list[tuple[str, str | None]] = []

    async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]:
        self.calls.append((req.url, job_id))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        await asyncio.sleep(0.01)
        self.inflight -= 1
        if req.url in self.fail:
            raise ServiceError.of(ErrorCode.FETCH_FAILED, "nope", retryable=True, source=req.url)
        return doc(req.url), False


def ctx(req: BatchFetchRequest, progress: list) -> JobContext:  # type: ignore[type-arg]
    async def persist(d: int, t: int, c: str | None) -> None:
        progress.append((d, t, c))
    return JobContext(job_id="J", request=req, emitter=Emitter(InMemoryEventBus(), "J"), _persist_progress=persist)


async def test_order_concurrency_partial() -> None:
    urls = tuple(f"https://s{i}.example/" for i in range(8))
    fake = FakeFetch(fail={urls[3]})
    progress: list = []  # type: ignore[type-arg]
    c = ctx(BatchFetchRequest(urls=urls), progress)
    result = await run_batch_fetch(c, fake, concurrency=3)  # type: ignore[arg-type]
    assert [d.url for d in result.documents] == [u for u in urls if u != urls[3]]
    assert [f.url for f in result.failed] == [urls[3]] and len(c.errors) == 1
    assert fake.peak <= 3 and progress[-1][:2] == (8, 8)
    assert all(job_id == "J" for _, job_id in fake.calls)


async def test_all_fail_raises() -> None:
    urls = ("https://a.example/", "https://b.example/")
    c = ctx(BatchFetchRequest(urls=urls), [])
    try:
        await run_batch_fetch(c, FakeFetch(fail=set(urls)), concurrency=2)  # type: ignore[arg-type]
    except ServiceError as exc:
        assert exc.detail.code is ErrorCode.FETCH_FAILED and "all 2 URLs failed" in exc.detail.message
    else:
        raise AssertionError("expected failure")
    assert len(c.errors) == 2


async def test_dedupes_canonical() -> None:
    urls = ("https://a.example/x?utm_source=1", "https://a.example/x")
    fake = FakeFetch()
    result = await run_batch_fetch(ctx(BatchFetchRequest(urls=urls), []), fake, concurrency=2)  # type: ignore[arg-type]
    assert len(fake.calls) == 1 and len(result.documents) == 2


async def test_endpoint_202(app, client: httpx.AsyncClient) -> None:  # noqa: ANN001
    r = await client.post("/v1/fetch/batch", json={"urls": ["https://blog.example/post"]})
    assert r.status_code == 202 and r.headers["location"] == f"/v1/jobs/{r.json()['data']['id']}"
    detail = (await client.get(r.headers["location"], params={"wait": 10})).json()["data"]
    assert detail["job"]["status"] == "done" and detail["result"]["documents"][0]["url"] == "https://blog.example/post"
    too_many = await client.post("/v1/fetch/batch", json={"urls": [f"https://a.example/{i}" for i in range(51)]})
    assert too_many.status_code == 422
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `run_batch_fetch` in `pipeline/search_read.py`**

```python
"""Job handlers: batch fetch (V1-07) and search-and-read (V1-08)."""

import asyncio
from typing import Protocol

from research_engine.errors import ServiceError
from research_engine.jobs.context import JobContext
from research_engine_client.models import (
    BatchFetchRequest, BatchFetchResult, Document, ErrorCode, FailedUrl, FetchRequest,
)

from .urls import canonicalize_url


class FetchesDocuments(Protocol):
    async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]: ...


async def _fetch_many(ctx: JobContext, fetch: FetchesDocuments, requests: list[FetchRequest], concurrency: int,
                      *, progress_offset: int = 0, progress_total: int | None = None
                      ) -> dict[str, Document | ServiceError]:
    """Fetch unique canonical URLs with bounded concurrency; returns canonical-url -> doc or error."""
    unique: dict[str, FetchRequest] = {}
    for r in requests:
        unique.setdefault(canonicalize_url(r.url), r)
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, Document | ServiceError] = {}
    done = 0
    total = progress_total if progress_total is not None else len(requests)

    async def one(canon: str, req: FetchRequest) -> None:
        nonlocal done
        async with sem:
            try:
                out[canon] = (await fetch.fetch(req, job_id=ctx.job_id))[0]
            except ServiceError as exc:
                out[canon] = exc
            done += 1
            await ctx.progress(progress_offset + done, total, current=req.url)

    await asyncio.gather(*(one(c, r) for c, r in unique.items()))
    return out


async def run_batch_fetch(ctx: JobContext, fetch: FetchesDocuments, *, concurrency: int) -> BatchFetchResult:
    req = ctx.request
    assert isinstance(req, BatchFetchRequest)
    requests = [FetchRequest(url=u, mode=req.mode, formats=req.formats, use_cache=req.use_cache,
                             timeout_s=req.timeout_s) for u in req.urls]
    outcomes = await _fetch_many(ctx, fetch, requests, concurrency, progress_total=len(requests))
    documents: list[Document] = []
    failed: list[FailedUrl] = []
    for r in requests:
        o = outcomes[canonicalize_url(r.url)]
        if isinstance(o, ServiceError):
            failed.append(FailedUrl(url=r.url, error=o.detail))
            ctx.add_error(o.detail)
        else:
            documents.append(o)
    if not documents:
        raise ServiceError.of(ErrorCode.FETCH_FAILED, f"all {len(requests)} URLs failed", retryable=True)
    return BatchFetchResult(documents=documents, failed=failed)
```

Deduplication counts progress per unique URL. When duplicates exist, `done` finishes below `total`. Fix this by calling `ctx.progress(total, total)` once at the end of `run_batch_fetch`; the test asserts the final value is `(8, 8)`.

- [ ] **Step 4: Implement `api/jobs_submit.py`**

```python
"""Job-submitting endpoints: POST /v1/fetch/batch (V1-07), POST /v1/search_read (V1-08)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request, Response, status

from research_engine_client.models import BatchFetchRequest, Envelope, Job, JobType

from .deps import Services, get_services
from .envelope import ok

router = APIRouter(prefix="/v1", tags=["jobs"])
BATCH_EXAMPLE = {"urls": ["https://docs.python.org/3/library/asyncio-task.html",
                          "https://www.rfc-editor.org/rfc/pdfrfc/rfc9110.txt.pdf"], "mode": "auto"}


@router.post("/fetch/batch", response_model=Envelope[Job], status_code=status.HTTP_202_ACCEPTED)
async def fetch_batch(request: Request, response: Response,
                      req: Annotated[BatchFetchRequest, Body(openapi_examples={"two": {"value": BATCH_EXAMPLE}})],
                      services: Annotated[Services, Depends(get_services)]) -> Envelope[Job]:
    job = await services.jobs.submit(JobType.FETCH_BATCH, req, session_id=req.session_id)
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    return ok(request, job)
```

- [ ] **Step 5: Register the handler in `app.py` (and `testing.py`).**

```python
runner.register(JobType.FETCH_BATCH,
                lambda ctx: run_batch_fetch(ctx, fetch, concurrency=settings.job_fetch_concurrency))
```

- [ ] **Step 6: Run the tests.** Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(jobs): batch fetch job and POST /v1/fetch/batch (V1-07)"`

---

### Task 5.2: Search-and-read job and `POST /v1/search_read`

**Goal:** Search, then fetch the top N results with bounded concurrency. Back-fill from lower-ranked results when a fetch fails, and return documents ranked by search score.

**Files:**
- Modify: `packages/research_engine/src/research_engine/pipeline/search_read.py` (add `run_search_read`)
- Modify: `packages/research_engine/src/research_engine/api/jobs_submit.py` (add the endpoint)
- Modify: `packages/research_engine/src/research_engine/app.py`, `testing.py` (register the handler)
- Test: `tests/service/unit/test_search_read.py`, `tests/integration/test_search_read_live.py`

**Acceptance Criteria:**
- [ ] The handler first runs `SearchService.search(req.search, job_id=ctx.job_id)` and reports progress `(0, top_n + 1, "search")`, then `(1, top_n + 1)`. If the search raises, the job fails.
- [ ] It fetches results in rank order until `top_n` documents succeed or candidates run out, trying at most `top_n * 2` URLs. Fetches run in waves of `concurrency`.
- [ ] `documents` is a list of `RankedDocument(search_rank, search_score, document)`, sorted by `search_rank` ascending. Failed URLs go to `failed` and `ctx.errors`, so the job ends `partial`.
- [ ] If the search has 0 results, the job ends `done` with empty documents. The `search.done` event reports `results=0`. That isn't an error.
- [ ] `POST /v1/search_read` returns 202 with `Envelope[Job]` and a `Location` header.
- [ ] Integration (`-m integration`, dev stack): `search_read` for `"compare open-source vector databases"` with `top_n=3` ends `done` or `partial` with at least 2 documents, and each document's `word_count` is at least 100.

**Verify:** `uv run pytest tests/service/unit/test_search_read.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.** Reuse the `FakeFetch` and `doc` helpers by moving them into `tests/service/unit/helpers.py`, and add a `FakeSearch`.

```python
# tests/service/unit/test_search_read.py
from research_engine.errors import ServiceError
from research_engine.pipeline.search_read import run_search_read
from research_engine_client.models import (
    ErrorCode, SearchReadRequest, SearchRequest, SearchResponse, SearchResult,
)

from .helpers import FakeFetch, make_ctx


class FakeSearch:
    def __init__(self, n: int, fail: bool = False) -> None:
        self.n, self.fail = n, fail

    async def search(self, req: SearchRequest, *, job_id: str | None = None) -> tuple[SearchResponse, bool]:
        if self.fail:
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "down", retryable=True)
        results = [SearchResult(rank=i, url=f"https://r{i}.example/", canonical_url=f"https://r{i}.example/",
                                title="t", snippet="s", domain=f"r{i}.example", engines=["a"], score=1 / i)
                   for i in range(1, self.n + 1)]
        return SearchResponse(query=req.query, results=results), False


async def test_backfill_and_ranking() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=3)
    fetch = FakeFetch(fail={"https://r2.example/"})
    progress: list = []  # type: ignore[type-arg]
    ctx = make_ctx(req, progress)
    res = await run_search_read(ctx, FakeSearch(10), fetch, concurrency=3)  # type: ignore[arg-type]
    assert [d.search_rank for d in res.documents] == [1, 3, 4]
    assert [f.url for f in res.failed] == ["https://r2.example/"] and len(ctx.errors) == 1
    assert len(fetch.calls) == 4


async def test_cap_attempts() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"), top_n=2)
    fetch = FakeFetch(fail={f"https://r{i}.example/" for i in range(1, 11)})
    res = await run_search_read(make_ctx(req, []), FakeSearch(10), fetch, concurrency=2)  # type: ignore[arg-type]
    assert len(fetch.calls) == 4 and res.documents == []


async def test_zero_results_done() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"))
    res = await run_search_read(make_ctx(req, []), FakeSearch(0), FakeFetch(), concurrency=2)  # type: ignore[arg-type]
    assert res.documents == [] and res.failed == []


async def test_search_failure_raises() -> None:
    req = SearchReadRequest(search=SearchRequest(query="q"))
    try:
        await run_search_read(make_ctx(req, []), FakeSearch(5, fail=True), FakeFetch(), concurrency=2)  # type: ignore[arg-type]
    except ServiceError:
        return
    raise AssertionError("expected ServiceError")
```

If every fetch fails, that's not raised as a job failure: `test_cap_attempts` expects an empty result. The job ends `partial`, because `ctx.errors` isn't empty. Search-and-read is a research primitive, so an empty-but-explained result is more useful to the agent than a failure. This differs deliberately from batch fetch; record the reason in the docstring.

Add an endpoint test like `test_endpoint_202` from 5.1, posting `{"search": {"query": "vector db", "depth": "quick"}, "top_n": 2}`.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement `run_search_read`.** Append to `pipeline/search_read.py`:

```python
class SearchesWeb(Protocol):
    async def search(self, req: SearchRequest, *, job_id: str | None = None) -> tuple[SearchResponse, bool]: ...


async def run_search_read(ctx: JobContext, search: SearchesWeb, fetch: FetchesDocuments, *,
                          concurrency: int) -> SearchReadResult:
    """Search, then read the top N results, back-filling past failures (max 2×N attempts).

    Unlike batch fetch, zero readable documents is not a job failure: the search response and the
    per-URL errors are still a useful, explained answer for the calling agent (job ends partial).
    """
    req = ctx.request
    assert isinstance(req, SearchReadRequest)
    total = req.top_n + 1
    await ctx.progress(0, total, current="search")
    response, _ = await search.search(req.search, job_id=ctx.job_id)
    await ctx.progress(1, total)
    candidates = response.results[: req.top_n * 2]
    documents: list[RankedDocument] = []
    failed: list[FailedUrl] = []
    idx = 0
    while len(documents) < req.top_n and idx < len(candidates):
        need = req.top_n - len(documents)
        wave = candidates[idx: idx + need]
        idx += len(wave)
        fetch_reqs = [FetchRequest(url=r.url, mode=req.fetch.mode, formats=req.fetch.formats,
                                   use_cache=req.fetch.use_cache, timeout_s=req.fetch.timeout_s) for r in wave]
        outcomes = await _fetch_many(ctx, fetch, fetch_reqs, concurrency,
                                     progress_offset=1 + len(documents), progress_total=total)
        for r in wave:
            o = outcomes[canonicalize_url(r.url)]
            if isinstance(o, ServiceError):
                failed.append(FailedUrl(url=r.url, error=o.detail))
                ctx.add_error(o.detail)
            else:
                documents.append(RankedDocument(search_rank=r.rank, search_score=r.score, document=o))
    documents.sort(key=lambda d: d.search_rank)
    await ctx.progress(total, total)
    return SearchReadResult(search=response, documents=documents, failed=failed)
```

Each wave fetches exactly the number of documents still needed, so back-filling is precise and `_fetch_many` bounds the concurrency. Add the imports for `SearchReadRequest`, `SearchReadResult`, `RankedDocument`, `SearchRequest` and `SearchResponse`.

Trace `test_backfill_and_ranking` (top_n=3, r2 fails):
- Wave 1 is r1, r2 and r3, which gives 2 documents.
- Wave 2 is r4 (need=1), which gives 3 documents.

That's 4 calls in total. ✓

- [ ] **Step 4: Add the endpoint to `api/jobs_submit.py`.** Use the same pattern as batch fetch, with `SearchReadRequest`, `JobType.SEARCH_READ` and the example `{"search": {"query": "compare open-source vector databases", "intent": "technical"}, "top_n": 5}`. Register the handler in `app.py`:

```python
runner.register(JobType.SEARCH_READ,
                lambda ctx: run_search_read(ctx, search, fetch, concurrency=settings.job_fetch_concurrency))
```

- [ ] **Step 5: Write the integration test.** `tests/integration/test_search_read_live.py` builds the full app through `create_app()`, using `SEARXNG_LIVE_URL` and `CRAWL4AI_LIVE_URL` and a temporary DB. It posts `search_read`, waits for the job, and asserts the criterion. Add the lifespan-managed app fixture `live_app` to `tests/integration/conftest.py`, so step 6 can reuse it.

- [ ] **Step 6: Run unit then integration.** Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(jobs): search-and-read job with back-fill and POST /v1/search_read (V1-08)"`

---

### Task 5.3: `/health`, `/version` and OpenAPI completeness

**Goal:** Add health and version endpoints for ops and the GUI, plus a test that every public endpoint carries an example and an envelope response.

**Files:**
- Create: `packages/research_engine/src/research_engine/api/health.py`
- Modify: `packages/research_engine/src/research_engine/api/deps.py` (add `health_checks: dict[str, Callable[[], Awaitable[bool]]]`)
- Modify: `packages/research_engine/src/research_engine/app.py`, `testing.py`
- Test: `tests/service/unit/test_health.py`, `tests/service/unit/test_openapi.py`

**Acceptance Criteria:**
- [ ] `GET /health` needs no auth and returns `Envelope[HealthReport]`.
  - **Dependencies:** `searxng` and `crawl4ai` come from adapter `health()`. `database` runs `SELECT 1`. `cache` is always `up`, with `detail="hit_rate=0.42 entries=N"`.
  - Each check runs concurrently under a 5 s timeout, and each records `latency_ms`. A timeout counts as `down` with `detail="timeout"`.
  - **Overall status:** `down` if `database` is down, `degraded` if `searxng` or `crawl4ai` is down, otherwise `up`.
  - **HTTP code:** 503 when the overall status is `down`, 200 otherwise. The Docker healthcheck depends on this.
  - The report includes `version` (the package `__version__`) and `git_sha` (`settings.git_sha`).
- [ ] `GET /version` needs no auth and returns `Envelope[VersionInfo]` (`version`, `git_sha`, `schema_version`).
- [ ] `GET /v1/schemas` returns the list of schema names. `GET /v1/schemas/{name}` returns the raw JSON Schema (not wrapped in an envelope, so tools can consume it directly; this exception is noted in the README) or 404 in an envelope. Both need auth.
- [ ] The OpenAPI test walks `/openapi.json` and checks two things:
  - every `POST` under `/v1` has at least one request body example;
  - every 2xx JSON response under `/v1`, `/health` and `/version` references a schema named `Envelope_*`, except `/v1/schemas/{name}`.
- [ ] Health emits no events, so the event log doesn't flood. The GUI health strip polls it.

**Verify:** `uv run pytest tests/service/unit/test_health.py tests/service/unit/test_openapi.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_health.py
import asyncio

import httpx


async def test_health_up_without_auth(app) -> None:  # noqa: ANN001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    body = r.json()["data"]
    assert r.status_code == 200 and body["status"] == "up"
    assert set(body["dependencies"]) == {"searxng", "crawl4ai", "database", "cache"}
    assert body["git_sha"] == "unknown"


async def test_degraded_and_timeout(app) -> None:  # noqa: ANN001
    async def down() -> bool:
        return False

    async def hang() -> bool:
        await asyncio.sleep(30)
        return True

    app.state.services.health_checks["searxng"] = down
    app.state.services.health_checks["crawl4ai"] = hang
    app.state.services.health_timeout_s = 0.1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    body = r.json()["data"]
    assert r.status_code == 200 and body["status"] == "degraded"
    assert body["dependencies"]["crawl4ai"]["detail"] == "timeout"


async def test_version(app) -> None:  # noqa: ANN001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/version")
    assert r.json()["data"]["schema_version"] == "1.0.0"


async def test_schemas(client: httpx.AsyncClient) -> None:
    names = (await client.get("/v1/schemas")).json()["data"]
    assert "Document" in names
    schema = (await client.get("/v1/schemas/Document")).json()
    assert schema["title"] == "Document"
    assert (await client.get("/v1/schemas/Nope")).status_code == 404
```

```python
# tests/service/unit/test_openapi.py
import httpx

EXEMPT = {"/v1/schemas/{name}"}


async def test_openapi_examples_and_envelopes(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    for path, ops in spec["paths"].items():
        if not (path.startswith("/v1") or path in ("/health", "/version")) or path in EXEMPT:
            continue
        for method, op in ops.items():
            if method == "post":
                content = op["requestBody"]["content"]["application/json"]
                assert content.get("examples") or content.get("example"), f"{path} lacks example"
            for code, resp in op["responses"].items():
                if code.startswith("2"):
                    ref = str(resp["content"]["application/json"]["schema"])
                    assert "Envelope_" in ref, f"{method.upper()} {path} {code} not enveloped: {ref}"
```

Pydantic names generic models such as `Envelope[SearchResponse]` as `Envelope_SearchResponse_` in OpenAPI. If FastAPI's naming differs (for example `Envelope[SearchResponse]`), adjust the assertion to match the generated name. Don't remove the check.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement `api/health.py`**

```python
"""GET /health, GET /version, GET /v1/schemas (V1-18, §4, §10)."""

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text

from research_engine import __version__
from research_engine.errors import ServiceError
from research_engine_client.models import (
    ALL_MODELS, SCHEMA_VERSION, DependencyHealth, DependencyState, Envelope, ErrorCode, HealthReport, VersionInfo,
)
from research_engine_client.schemas import render_schemas

from .deps import Services, get_services
from .envelope import ok

router = APIRouter(tags=["ops"])


async def _timed(check, timeout_s: float) -> DependencyHealth:  # noqa: ANN001
    t0 = time.perf_counter()
    try:
        up = await asyncio.wait_for(check(), timeout_s)
    except TimeoutError:
        return DependencyHealth(state=DependencyState.DOWN, latency_ms=int(timeout_s * 1000), detail="timeout")
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return DependencyHealth(state=DependencyState.DOWN, detail=type(exc).__name__)
    return DependencyHealth(state=DependencyState.UP if up else DependencyState.DOWN,
                            latency_ms=int((time.perf_counter() - t0) * 1000))


@router.get("/health", response_model=Envelope[HealthReport])
async def health(request: Request, response: Response,
                 services: Annotated[Services, Depends(get_services)]) -> Envelope[HealthReport]:
    async def db() -> bool:
        async with services.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    checks = {**services.health_checks, "database": db}
    results = await asyncio.gather(*(_timed(c, services.health_timeout_s) for c in checks.values()))
    deps = dict(zip(checks, results, strict=True))
    n, _ = await services.cache.size()
    deps["cache"] = DependencyHealth(state=DependencyState.UP,
                                     detail=f"hit_rate={services.cache.stats().hit_rate:.2f} entries={n}")
    if deps["database"].state is DependencyState.DOWN:
        overall = DependencyState.DOWN
    elif any(deps[k].state is DependencyState.DOWN for k in ("searxng", "crawl4ai") if k in deps):
        overall = DependencyState.DEGRADED
    else:
        overall = DependencyState.UP
    if overall is DependencyState.DOWN:
        response.status_code = 503
    return ok(request, HealthReport(status=overall, version=__version__, git_sha=services.settings.git_sha,
                                    dependencies=deps))


@router.get("/version", response_model=Envelope[VersionInfo])
async def version(request: Request, services: Annotated[Services, Depends(get_services)]) -> Envelope[VersionInfo]:
    return ok(request, VersionInfo(version=__version__, git_sha=services.settings.git_sha,
                                   schema_version=SCHEMA_VERSION))


@router.get("/v1/schemas", response_model=Envelope[list[str]], tags=["schemas"])
async def list_schemas(request: Request) -> Envelope[list[str]]:
    return ok(request, sorted(ALL_MODELS))


@router.get("/v1/schemas/{name}", tags=["schemas"])
async def get_schema(name: str) -> Response:
    rendered = render_schemas()
    if name not in rendered:
        raise ServiceError.of(ErrorCode.NOT_FOUND, f"no schema {name}", retryable=False, http_status=404)
    return Response(content=rendered[name], media_type="application/schema+json")
```

Add `health_checks: dict[str, Callable[[], Awaitable[bool]]]` and `health_timeout_s: float = 5.0` to `Services`. In `build_services`, set `health_checks={"searxng": provider.health, "crawl4ai": browser.health}`. `testing.py` uses fakes that return True.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Run the full step check** (Global Constraint 11) and regenerate the schemas if any model changed (`uv run research-engine schemas export --check`).

- [ ] **Step 6: Commit.** `git commit -m "feat(api): /health, /version, /v1/schemas and OpenAPI completeness test (V1-14, V1-18)"`

---

**End of step 5:** request code review, report to the owner with evidence (unit plus integration output), and **stop** for approval. After approval, run `git push`.
