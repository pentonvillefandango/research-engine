# Research Engine V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build V1 of the Research Engine. It is a self-hosted FastAPI service that gives research agents multi-engine web search, high-quality page reading and a live activity GUI, over REST and MCP, returning versioned Pydantic models. It makes no model calls.

**Architecture:** The repo is a uv workspace with two packages:
- `research_engine_client`: the shared Pydantic models (the contract) and an async client.
- `research_engine`: the FastAPI service.

The service puts every external tool behind an adapter protocol: SearXNG (`SearchProvider`), httpx and Crawl4AI (`Fetcher`), and Trafilatura, extruct, lxml and pypdf (extractors). An orchestrator applies politeness, SSRF, cache and event rules. Jobs, events and cache live in SQLite (WAL). The GUI uses Jinja2, vendored htmx and FastAPI-native SSE. Everything ships as one Docker Compose project behind a shared Caddy.

**Tech Stack:**
- Python ≥3.12 (runtime 3.13), uv 0.12.
- Web and data: FastAPI 0.142, Pydantic 2.13, pydantic-settings 2.15, SQLModel 0.0.47 with aiosqlite.
- Fetching and extraction: httpx 0.28, Trafilatura 2.3, extruct 0.18, lxml 6.1, pypdf 6.19, Protego 0.7.
- Agent integration: mcp 2.3 (`MCPServer`).
- Logging and GUI: structlog 26, Jinja2, nh3, markdown-it-py, htmx 2.0.11 with htmx-ext-sse 2.2.4.
- Tests and linting: pytest 9.1, pytest-asyncio 1.4, respx 0.23, ruff 0.16, pyright 1.1.414.
- Containers: SearXNG `2026.10.4-d48c4b555`, Crawl4AI `0.9.4`, Caddy `2.11.6`.
- Security tooling: gitleaks v8.30.1.

**Spec:**
- `REQUIREMENTS.md`, the primary spec.
- `docs/superpowers/specs/2026-10-04-v1-design.md`, the design addendum. It wins where the two differ.

**Step files:** each build step from REQUIREMENTS §11 has its own detailed file under `docs/plans/v1/`. This index holds the constraints and the shared interface contract that every step relies on. Read this file first, then the step file.

| Step | File | Branch | Feature IDs |
|---|---|---|---|
| 0 Public-repo hygiene | `v1/step-0-repo-hygiene.md` | `main` | V1-19, §8 |
| 1 Models + JSON Schema export | `v1/step-1-models.md` | `v1` | §4, V1-14 |
| 2 SearXNG adapter + `/v1/search` | `v1/step-2-search.md` | `v1` | V1-01, V1-02, V1-03, V1-18 |
| 3 Fetch pipeline | `v1/step-3-fetch.md` | `v1` | V1-04, V1-05, V1-06, V1-11 |
| 4 SQLite store, jobs, cache, events | `v1/step-4-store-jobs.md` | `v1` | V1-09, V1-10, V1-15 |
| 5 `/v1/search_read` + batch fetch | `v1/step-5-search-read.md` | `v1` | V1-07, V1-08, V1-14, V1-18 |
| 6 GUI | `v1/step-6-gui.md` | `v1` | V1-16, V1-17, V1-21 |
| 7 MCP server + typed client | `v1/step-7-mcp-client.md` | `v1` | V1-12, V1-13 |
| 8 Compose, SearXNG settings, Caddy | `v1/step-8-compose-caddy.md` | `v1` | V1-20 |
| 9 Ops, Makefile, docs | `v1/step-9-ops-docs.md` | `v1` | V1-22 |

---

## Global Constraints

Every task and every reviewer must hold to these.

1. **No model or LLM calls in V1** (D5). Never add `litellm`, `openai`, `anthropic` or `instructor` to the service package. `openai-agents` is allowed **only** as a dev dependency for tests and examples.
2. **No secrets or lab-specific values committed.**
   - No IPs, real hostnames other than the documented example `research.toolbox.home.arpa`, usernames, API keys or tokens.
   - All of these come from `.env`, with neutral defaults (`SITE_HOST=research.localhost`).
   - gitleaks must pass on every commit.
3. **Never configure Chromium with `--no-sandbox`** (§12 and B3).
4. **Every external tool sits behind its adapter protocol** (`SearchProvider`, `Fetcher`, `HtmlExtractor`/`PdfExtractor`). Orchestration code imports protocols, never concrete adapters (only the composition root `app.py` wires concrete classes).
5. **Every API response is `Envelope[T]`** from `research_engine_client.models`.
   - Optional fields are explicitly `X | None = None` and always serialised (never `exclude_none`).
   - Use enums wherever the set of values is known.
   - `SCHEMA_VERSION = "1.0.0"`.
6. **Async throughout.**
   - No blocking calls on the event loop. CPU-bound or blocking libraries (Trafilatura, extruct, lxml, pypdf, Protego parsing) run via `asyncio.to_thread`.
   - Our HTTP calls use **httpx** (not httpx2), so respx can mock them.
7. **A failed engine or page never fails the whole request or job.** The job ends as `partial` with typed `errors[]`.
8. **TDD.**
   - Write the failing test, watch it fail, then write the minimal code to pass.
   - Unit tests never touch the network: respx and fixtures only.
   - Integration tests carry `@pytest.mark.integration` and are skipped unless `-m integration` is passed.
9. **Co-tenancy (§9).**
   - No fixed host ports and no `container_name`.
   - No `docker system prune` or other VM-wide clean-up.
   - No writes outside `/opt/research-engine/` and this project's Docker volumes.
   - Compose project name `research-engine`.
10. **Commit messages** begin with a conventional prefix and include the feature IDs, e.g. `feat(search): SearXNG adapter (V1-01, V1-03)`, and end with the attribution trailer from the session.
11. **Every step finishes with `uv run ruff check`, `uv run ruff format --check`, `uv run pyright` and `uv run pytest`, all green.** Paste the output when reporting the step.
12. **Pause for the owner's confirmation after each step.** Push only after approval (B5).
13. **No chains of foreground sleep-and-check tool calls while waiting on subagents** (per the owner's global CLAUDE.md). Use `docker compose up -d --wait` for container readiness, and a single `run_in_background` `until` loop for anything else. Short bounded waits inside scripts (e.g. `scripts/check_sandbox.sh`) are fine.

**User decisions (already made):**
- B1: The working method is Superpowers plus REQUIREMENTS.md. There is no Jira; V1-xx IDs are the ticket references. Ignore `/mydocs`.
- B2: GUI auth is a login page that sets an HttpOnly session cookie. The API accepts the `X-API-Key` header **or** that cookie. Caddy restricts access to `LAB_SUBNET`.
- B3: Keep the Chromium sandbox. Spike in step 3. The fallback is a minimal per-service seccomp profile with an ADR; if that also fails, stop and ask.
- B4: `requires-python >=3.12`; runtime image `python:3.13.16-slim-trixie`.
- B5: Step 0 goes on `main`. Steps 1–9 go on branch `v1`, with a push after each approved step and CI on every push. One PR `v1`→`main` at the end, then tag `v1.0.0`. Step 0 enables secret scanning, push protection and Dependabot.
- B6: OpenAI Agents SDK coverage has two parts: an automated model-free MCP test, and a manual `examples/openai_agents_mcp.py` using `OPENAI_API_KEY` (which skips when the key is unset).
- B7: Names stay `research-engine`, `research_engine` and `research_engine_client`. LICENSE: `Copyright (c) 2026 pentonvillefandango`.

---

## Shared Interface Contract

Later steps rely on these names and signatures exactly. A task that needs to change one must update this section and every caller in the same commit.

### Public models: `packages/research_engine_client/src/research_engine_client/models/`

| Module | Names |
|---|---|
| `common.py` | `SCHEMA_VERSION`, `ErrorCode` (StrEnum), `ErrorDetail`, `Meta`, `Envelope[T]` |
| `search.py` | `SearchIntent`, `SearchDepth`, `TimeRange`, `SearchRequest`, `SearchResult`, `InfoboxLink`, `Infobox`, `UnresponsiveEngine`, `SearchResponse`, `IntentPreset`, `EngineInfo`, `EnginesResponse` |
| `document.py` | `FetchMode`, `DocumentFormat`, `FetchMethod`, `EscalationReason`, `FetchRequest`, `Link`, `Table`, `StructuredData`, `Provenance`, `Quality`, `Document` |
| `jobs.py` | `JobType`, `JobStatus`, `JobProgress`, `Job`, `BatchFetchRequest`, `SearchReadRequest`, `FailedUrl`, `BatchFetchResult`, `RankedDocument`, `SearchReadResult`, `JobDetail` |
| `events.py` | `EventLevel`, `EventKind`, `Event` |
| `health.py` | `DependencyState`, `DependencyHealth`, `HealthReport`, `VersionInfo` |
| `__init__.py` | re-exports everything above; `ALL_MODELS: dict[str, type[BaseModel]]` (the schema registry) |

### Service internals: `packages/research_engine/src/research_engine/`

```python
# config.py
class Settings(BaseSettings): ...           # all env vars, see step 2 Task 2.2
def get_settings() -> Settings: ...         # lru_cache'd

# errors.py
class ServiceError(Exception):
    def __init__(self, detail: ErrorDetail, http_status: int = 502) -> None: ...
    detail: ErrorDetail
    http_status: int
    upstream_status: int | None      # HTTP status from the fetched site, when known (drives escalation)

# events/base.py
class EventSink(Protocol):
    async def emit(self, event: Event) -> None: ...
class EventSubscriber(Protocol):
    def subscribe(self) -> AsyncIterator[Event]: ...   # async generator; cancels cleanly
class Emitter:                               # convenience wrapper bound to a job
    def __init__(self, sink: EventSink, job_id: str | None = None) -> None: ...
    def bind(self, job_id: str | None) -> "Emitter": ...
    async def debug/info/warning/error(self, kind: EventKind, message: str, **data: Any) -> None: ...
# events/memory.py
class InMemoryEventBus(EventSink, EventSubscriber): ...  # bounded per-subscriber queues (maxsize 1000, drop-oldest)
# events/sqlite.py   (step 4)
class SqliteEventBus(EventSink, EventSubscriber): ...    # persist, then fan out via InMemoryEventBus

# cache/base.py
class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl_s: int) -> None: ...
    def stats(self) -> CacheStats: ...       # dataclass(hits:int, misses:int)
def cache_key(kind: str, payload: BaseModel | str) -> str: ...  # sha256 hex of kind + canonical JSON
# cache/memory.py: InMemoryCache   # cache/sqlite.py (step 4): SqliteCache

# adapters/search.py
@dataclass RawHit(url, title, content, engines: list[str], positions: list[int], published: datetime | None)
@dataclass RawSearchPage(hits: list[RawHit], suggestions: list[str], infoboxes: list[Infobox], unresponsive: list[UnresponsiveEngine])
# (RawHit.positions are already offset by (pageno-1)*10 in the adapter)
class SearchProvider(Protocol):
    async def search(self, query: str, *, categories: list[str], engines: list[str],
                     language: str, time_range: TimeRange | None, pageno: int) -> RawSearchPage: ...
    async def health(self) -> bool: ...
# adapters/searxng.py: SearxngProvider(base_url: str, client: httpx.AsyncClient, timeout_s: float)

# adapters/fetch.py
@dataclass RawPage(url, final_url, status: int, content_type: str, body: bytes,
                   html: str | None, markdown: str | None, method: FetchMethod,
                   redirects: list[str])
class Fetcher(Protocol):
    async def fetch(self, url: str, *, timeout_s: float, on_hop: HopHook | None = None) -> RawPage: ...
    async def health(self) -> bool: ...
# HopHook: async callback around EVERY request the fetcher sends (first request of each attempt and each redirect hop); FetchService uses it for per-hop robots + the single per-stage limiter slot
# adapters/static_fetch.py: StaticFetcher(client, guard: SsrfGuard, settings)
# adapters/crawl4ai.py:     Crawl4AIFetcher(base_url, token, client, guard: SsrfGuard)

# adapters/extract.py
@dataclass Extracted(title, author, published_at, language, markdown, word_count, links: list[Link],
                     tables: list[Table], structured_data: StructuredData, html_len: int, text_len: int,
                     warnings: list[str] = [])   # truncation/degradation notes; FetchService copies into Document.warnings
class HtmlExtractor(Protocol):  def extract(self, html: str, base_url: str) -> Extracted: ...   # sync; caller uses to_thread
class PdfExtractor(Protocol):   def extract(self, body: bytes, url: str) -> Extracted: ...
# adapters/html_extract.py: DefaultHtmlExtractor   adapters/pdf_extract.py: PypdfExtractor

# safety/ssrf.py
class SsrfGuard:
    def __init__(self, allow_hosts: frozenset[str], resolver: Resolver | None = None) -> None: ...
    async def check(self, url: str) -> httpx.URL: ...   # returns the parsed, userinfo-free URL to request; raises ServiceError(code=ssrf_blocked)
# safety/robots.py
class RobotsPolicy:
    async def check(self, url: str, em: Emitter | None = None) -> None: ...   # raises ServiceError(code=robots_disallowed); applies crawl-delay via DomainLimiter.set_delay
# safety/limiter.py
class DomainLimiter:
    def __init__(self, concurrency: int, delay_s: float, *, clock=..., sleep=...) -> None: ...
    def slot(self, url: str) -> AbstractAsyncContextManager[None]: ...  # per-domain semaphore + min interval
# limiter_key(url) -> str: the slot key = httpx IDNA-2008 punycode host minus "www."; RobotsPolicy sets crawl-delay on the same key

# pipeline/urls.py:   canonicalize_url(url: str) -> str (search dedupe) ; domain_of(url: str) -> str ;
#                     fetch_cache_url(url: str) -> str (page cache: keeps ref, #/ and #! fragments, path/query as given)
# pipeline/fetch.py:  page_cache_key(req: FetchRequest) -> str (formats order-normalised, timeout_s excluded)
# pipeline/ranking.py: merge_and_score(pages: list[RawSearchPage], max_results: int) -> list[SearchResult]
# pipeline/search.py: SearchService(provider, intents: IntentRegistry, cache, events: EventSink, settings)
#                     async def search(self, req: SearchRequest, *, job_id: str | None = None) -> tuple[SearchResponse, bool]  # (response, cache_hit)
# pipeline/fetch.py:  FetchService(static: Fetcher, browser: Fetcher, html: HtmlExtractor, pdf: PdfExtractor,
#                                  robots: RobotsChecker, limiter: DomainLimiter, cache, events: EventSink, settings)
#                     (RobotsChecker: Protocol in pipeline/fetch.py with RobotsPolicy.check's signature; RobotsPolicy implements it)
#                     async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]
# pipeline/search_read.py (step 5): run_search_read(...), run_batch_fetch(...)

# jobs/store.py (step 4):  JobStore   jobs/runner.py: JobRunner
#   async def submit(self, type: JobType, request: BaseModel, *, session_id: str | None = None) -> Job
#   async def get(self, job_id: str) -> JobDetail | None
#   async def wait(self, job_id: str, timeout_s: float) -> JobDetail | None
#   async def cancel(self, job_id: str) -> Job | None
#   register(type: JobType, handler: JobHandler)   # JobHandler = Callable[[JobContext], Awaitable[BaseModel]]

# api/deps.py: Services dataclass on app.state.services, get_services() dependency.
#   Fields grow per step (step 2: settings, intents, events, cache, search; step 3: fetch; step 4: engine, jobs, job_store;
#   step 5: health_checks, health_timeout_s). Required fields always precede defaulted ones (http, fetch_http, extra, health_timeout_s).
#   http: app client for internal services (SearXNG, Crawl4AI); fetch_http: safety.http.make_fetch_client (robots + static fetcher).
#   testing.build_test_services(settings) mirrors build_services with fakes + ':memory:' SQLite and is updated in the same task.
# api/envelope.py: ok(request: Request, data, *, cache_hit=False) -> Envelope  (request_id + start time set on request.state by RequestContextMiddleware); error envelopes via exception handlers
# api/auth.py: ApiKeyMiddleware(app, api_key)  -> step 6: ApiKeyMiddleware(app, api_key, codec: SessionCodec, site_host)
#   (pure ASGI; X-API-Key header OR signed re_session cookie + Origin check; GUI paths redirect to /login)
# config_files.py: IntentRegistry.load(path) -> IntentRegistry; .get(intent) -> IntentPreset; .all() -> dict[SearchIntent, IntentPreset]
# app.py: create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI
#         build_fetch_service(settings, *, http, fetch_http, cache, events) -> FetchService  (also used by the live tests)
```

### Error codes (`ErrorCode`)
`invalid_request`, `unauthorized`, `not_found`, `ssrf_blocked`, `robots_disallowed`, `content_type_not_allowed`, `response_too_large`, `upstream_timeout`, `upstream_error`, `engine_failed`, `fetch_failed`, `extraction_failed`, `job_cancelled`, `job_timeout`, `interrupted`, `internal_error`.

### Event kinds (`EventKind`)
`search.started`, `search.engine_failed`, `search.retry_broader`, `search.done`, `fetch.started`, `fetch.static_done`, `fetch.escalated`, `fetch.browser_done`, `fetch.failed`, `fetch.done`, `robots.disallowed`, `robots.fetched`, `cache.hit`, `cache.miss`, `job.queued`, `job.started`, `job.progress`, `job.done`, `job.partial`, `job.failed`, `job.cancelled`, `system.startup`, `system.shutdown`, `system.health`.

---

## Testing conventions

- **Layout:**
  - `tests/client/` tests the models and client.
  - `tests/service/unit/...` tests the service offline.
  - `tests/integration/` is marked.
  - Fixtures live in `tests/fixtures/` (`searxng/*.json`, `crawl4ai/*.json`, `pages/*.html`, `pages/*.pdf`).
- `pyproject.toml` (root) sets the following:
  - `[tool.pytest]` with `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`, `markers = ["integration: needs the live compose stack"]` and `addopts = ["-m", "not integration", "--strict-markers"]`.
  - `[tool.ruff.lint] select = ["E","F","W","I","B","UP","ASYNC","S","SIM","RUF"]`, with `S101` ignored in tests.
  - `[tool.pyright]` with `typeCheckingMode = "standard"`, and `strict` for `packages/research_engine_client`.
- **Test packages:** every directory under `tests/` has an `__init__.py`, so duplicate basenames are fine and relative imports (`from .helpers import ...`) work.
- **Shared fixtures** in `tests/conftest.py`:
  - `settings` gives a `Settings` with test values.
  - `app` and `client` give an `httpx.AsyncClient(transport=ASGITransport(app))` with the `X-API-Key: test-key` header.
- **Recording real fixtures:** step 2 Task 2.1 adds `scripts/record_fixtures.py`, which runs against the minimal compose stack. Fixtures are committed only after a manual check that they hold no lab-specific data.

## Execution notes for the controller

- Each step's tasks are listed in order with dependencies. Within a step, tasks touching disjoint files may run as parallel subagents. Tasks that touch `app.py`, `api/deps.py` or `config.py` are serialised.
- After the last task of a step:
  1. Run the full verification (Global Constraint 11).
  2. Request code review (`superpowers-extended-cc:requesting-code-review`).
  3. Commit any review fixes.
  4. Report to the owner with evidence.
  5. **Stop** until the owner approves.
  6. Push.
- Ops commands that restart the live stack (`make deploy|rollback|restore|bootstrap`) need the owner's approval every time.
