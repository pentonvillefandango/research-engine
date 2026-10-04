# Research Engine – Web Search & Scrape Service

**Requirements and build guide for Claude Code**
Last updated: 2026-10-04 · Status: Agreed for V1 build

---

## 1. Purpose and principles

Build a self-hosted web search and scraping service that research agents call over REST and MCP. It returns typed, provenance-tagged structured data. Research quality comes before speed.

**Where it sits.** In the software factory's requirements engine, basic user requirements go to an agent backed by a frontier model for investigation. That agent needs to search the web, read pages and pull out facts. This service is its eyes and hands on the web. The reasoning stays in the calling agent; this service finds, fetches, cleans and structures.

**Design principles**

- **No paid infrastructure.** Search, fetching and storage use only open-source components self-hosted in Docker on the Proxmox lab, with no paid search or scraping APIs. Model calls are the one place spend is allowed, through a model selector covering paid APIs and local Ollama (from V2).
- **Quality over speed.** Query several engines, read full pages rather than snippets, escalate to a real browser when a page is thin, and rerank. Generous timeouts are fine.
- **Structured first.** Every response is a versioned Pydantic model with a published JSON Schema. No endpoint returns free text only.
- **Provenance on everything.** Each piece of content carries its URL, fetch time, content hash and extraction method, so an agent can cite and re-check it.
- **Reuse, don't rebuild.** Wrap mature open-source tools. Own only the orchestration, ranking, schemas, events and GUI.
- **Framework-agnostic.** REST with OpenAPI plus an MCP server, so it plugs into raw Python, OpenAI Agents SDK, Claude Agent SDK, LangGraph and CrewAI alike.
- **Observable.** Every step emits a structured event that the GUI shows live.
- **Polite and safe.** Respect robots.txt, rate-limit per domain, never solve CAPTCHAs, and treat all fetched content as untrusted.

---

## 2. Decisions log

| # | Decision | Reason |
| --- | --- | --- |
| D1 | Build on **SearXNG** (search) + **Crawl4AI** (browser fetch) behind our own FastAPI service | Free, lighter than self-hosted Firecrawl, and we own the orchestration and schemas |
| D2 | **Not** using self-hosted Firecrawl | Heavier stack; anti-bot, /agent and /browser features are cloud-only anyway |
| D3 | No paid search or scraping APIs, ever | Keep infrastructure cost at zero |
| D4 | Model API spend is acceptable, via a **model selector** (LiteLLM) with per-task routing and a budget cap | Different tasks suit different models; spend stays visible and capped |
| D5 | **V1 makes no model calls**; AI features start in V2 | Search, fetch and extraction of tables/JSON-LD are deterministic; reasoning lives in the calling agent |
| D6 | Backend is a **uv**-based Python project (3.12+, `src/` layout, uv workspace) | Stated requirement |
| D7 | GUI is **FastAPI + HTMX + server-sent events** | Live updates with no JS build step; stays in Python |
| D8 | V1 GUI includes a **test console** that calls the public API | Show value without code; doubles as a smoke test |
| D9 | **SQLite (WAL)** for jobs, events, cache and results in V1 | Simple, no extra service |
| D10 | VPN egress via **Gluetun** HTTP-proxy profiles, **V2**, optional, direct is default | Region-specific results and protecting the home IP; VPN IPs attract more CAPTCHAs so not default |
| D11 | Deploy as **one Docker Compose project** | Single unit to deploy and manage; optional services behind Compose profiles |
| D12 | Run on a **Debian VM** on Proxmox, **not an LXC**; the VM (`toolbox`) hosts this and other tools | Untrusted pages in a headless browser: own kernel, Chromium sandbox works without `--no-sandbox`, Gluetun TUN works without passthrough. The stack must therefore be a good co-tenant (see section 9) |
| D13 | VM has **QEMU guest agent** enabled in Proxmox and installed in the guest | Clean shutdowns, snapshots and IP reporting |
| D14 | AGPL components (SearXNG) run as separate containers, called over HTTP only | Clean licence boundary |
| D15 | A **shared Caddy** runs on the tools VM as its own Compose project (`/opt/caddy/`); this repo ships its config | No Caddy exists elsewhere in the lab yet; one proxy serves every tool on the VM by hostname |
| D16 | VM resources are **not a constraint**; grow the VM as needed | Spare CPU and RAM can be allocated at any time; per-service limits still apply so tools can't starve each other |
| D17 | VM hostname **`toolbox`**; tools are served as subdomains (`research.toolbox`) via Caddy | One memorable host; DNS on the UniFi Dream Router as one A record for `toolbox` plus a CNAME per tool |
| D18 | **One environment:** develop and run on `toolbox`; no separate dev or staging stack | Simplicity; commits, smoke tests and automatic rollback provide the safety net |
| D19 | Claude Code runs **on `toolbox`** and operates the live stack through scripted, idempotent ops commands with JSON output; commands that restart or restore the service need the owner's approval | Safe, verifiable DevOps by an agent on the same host it develops on |
| D20 | The GitHub repo is **public** and meant to be shared | Code must be reusable by others and must never contain secrets or lab-specific values (see section 8) |

---

## 3. Architecture and reused components

A uv-based Python service (FastAPI) sits between the research agents and a set of open-source engines. It exposes REST and MCP, runs work as jobs, stores results and events in SQLite, and streams activity to a built-in GUI.

```
        Research agents (requirements engine, any framework)        Your browser
                 │ REST                 │ MCP                              │ watches
 ┌───────────────▼──────────────────────▼──────────────────────────────────▼──────┐
 │  Research Engine service (uv, FastAPI)                                         │
 │                                                                                │
 │   REST API              MCP server                  Activity GUI + Test console│
 │      │                      │                              ▲ live updates (SSE)│
 │      └──────────┬───────────┘                              │                   │
 │                 ▼                                          │                   │
 │   Job runner & orchestrator ──── events ────────────▶ Event bus                │
 │   (queue, dedupe, rank, politeness)                        │ stores            │
 │                 │                                          ▼                   │
 │                 ▼                                     SQLite store             │
 │   Adapters: SearchProvider, Fetcher, Extractor        (jobs, events, cache)    │
 │   (Trafilatura, extruct in-process)                                            │
 └──────┬──────────────────┬───────────────────┬────────────────────────────────────┘
        ▼                  ▼                   ┆ (V2)
     SearXNG            Crawl4AI           Models: APIs via LiteLLM, or Ollama
   (metasearch)    (headless browser)
        │                  │        ┆ optional egress via Gluetun proxies (V2)
        ▼                  ▼
          The public web: search engines, websites, free APIs
```

Agents never touch the web directly: every request passes through the orchestrator's politeness rules and the adapters, and every step lands in the event log. Each external engine sits behind an adapter interface (`SearchProvider`, `Fetcher`, `Extractor`), so any one can be swapped without touching the rest.

| Component | Role in the service | Licence | Version |
| --- | --- | --- | --- |
| [SearXNG](https://github.com/searxng/searxng) | Metasearch across many engines via its JSON API | AGPL-3.0 (called over HTTP only) | V1 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | Headless-browser fetching, JS rendering, clean markdown | Apache-2.0 | V1 |
| [Trafilatura](https://github.com/adbar/trafilatura) | Fast main-text and metadata extraction for static pages | Apache-2.0 | V1 |
| [extruct](https://github.com/scrapinghub/extruct) | Pulls JSON-LD, microdata and OpenGraph from pages | BSD-3 | V1 |
| [Protego](https://github.com/scrapy/protego) | robots.txt parsing | BSD-3 | V1 |
| FastAPI, Pydantic v2, pydantic-settings | API, schemas, config | MIT | V1 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MCP server with structured tool output | MIT | V1 |
| SQLite (WAL) with SQLModel | Jobs, events, cache, results | Public domain / MIT | V1 |
| HTMX with server-sent events | GUI with live updates, no JS build | BSD-2 | V1 |
| [LiteLLM](https://github.com/BerriAI/litellm) | Model gateway behind the model selector (Anthropic, OpenAI, Google, Ollama) | MIT | V2 |
| [Ollama](https://github.com/ollama/ollama) | Free local option in the model selector | MIT | V2 |
| [Instructor](https://github.com/567-labs/instructor) | LLM output validated into Pydantic models | MIT | V2 |
| [Docling](https://github.com/docling-project/docling) | PDF, DOCX, PPTX to structured sections and tables | MIT | V2 |
| [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank) or a BGE cross-encoder | Local reranking of passages | Apache-2.0 | V2 |
| [Gluetun](https://github.com/qdm12/gluetun) | Optional VPN egress profiles via HTTP proxy | MIT | V2 |
| Free public APIs | GitHub, PyPI, npm, Stack Exchange, arXiv, Wikipedia, Wikidata, OSV.dev, Wayback Machine | Free tiers | V2 |
| [LanceDB](https://github.com/lancedb/lancedb) | Embedded vector store for past research | Apache-2.0 | V3 |
| [Camoufox](https://github.com/daijro/camoufox) | Hardened browser profile for difficult sites | MPL-2.0 | V3 |
| OpenTelemetry | Traces across research jobs | Apache-2.0 | V3 |

---

## 4. Structured data contracts

The Pydantic models are the product: they live in their own importable package (`research_engine_client.models`, shared with the service) so agent frameworks use the same types as the service.

| Model | Purpose | Key fields | Version |
| --- | --- | --- | --- |
| `Envelope[T]` | Wraps every API response | `data`, `meta` (request_id, schema_version, took_ms, cache_hit), `errors[]` | V1 |
| `SearchRequest` | A search query | `query`, `intent`, `max_results`, `time_range`, `language` (default en-GB), `engines?`, `depth`, `egress?` (V2) | V1 |
| `SearchResult` | One deduplicated hit | `rank`, `url`, `canonical_url`, `title`, `snippet`, `domain`, `engines[]`, `score`, `published_at?` | V1 |
| `SearchResponse` | All hits for a query | `query`, `results[]`, `suggestions[]`, `infoboxes[]`, `unresponsive_engines[]` | V1 |
| `FetchRequest` | Read one URL | `url`, `mode` (auto, static, browser), `formats[]`, `use_cache`, `timeout_s`, `egress?` (V2) | V1 |
| `Document` | A cleaned page | `url`, `final_url`, `status`, `title`, `author?`, `published_at?`, `language`, `markdown`, `word_count`, `links[]`, `tables[]`, `structured_data`, `provenance`, `quality`, `warnings[]` | V1 |
| `Table` | A table found on a page | `caption?`, `headers[]`, `rows[][]`, `source_selector?` | V1 |
| `StructuredData` | Embedded page metadata | `json_ld[]`, `microdata[]`, `opengraph{}` | V1 |
| `Provenance` | Where content came from | `url`, `fetched_at`, `content_hash`, `method` (static, browser, api, archive), `egress` (V2), `job_id` | V1 |
| `Job` | Unit of async work | `id`, `type`, `status` (queued, running, partial, done, failed, cancelled), `progress`, `parent_id?`, `session_id?`, `request`, `result_ref?`, `errors[]` | V1 |
| `Event` | One log line | `ts`, `job_id?`, `level`, `kind` (e.g. `search.engine_failed`), `message`, `data{}` | V1 |
| `ErrorDetail` | A typed failure | `code`, `message`, `retryable`, `source?` | V1 |
| `Passage` | A ranked chunk of a document | `document_id`, `text`, `heading_path[]`, `char_span`, `score` | V2 |
| `Extraction[T]` | Schema-driven extraction result | `schema_name`, `items[]` of T, `field_evidence{}` (quote and char span per field), `valid`, `validation_errors[]` | V2 |
| `Claim` | An atomic fact | `statement`, `value?`, `unit?`, `evidence[]` (source and short quote), `confidence` | V2 |
| `SourceAssessment` | How far to trust a source | `domain_class` (official, vendor, standards, community, news, unknown), `freshness`, `quality_signals[]`, `score` | V2 |
| `ModelCall` | One LLM call | `task`, `model`, `tokens_in`, `tokens_out`, `cost`, `latency_ms`, `job_id` | V2 |
| `ResearchReport` | Output of a deep research run | `brief`, `findings[]`, `claims[]`, `conflicts[]`, `open_questions[]`, `sources[]`, `coverage`, `confidence` | V3 |
| `Entity`, `Relation` | Knowledge graph | `type` (library, product, vendor, standard, org), `name`, `attributes{}`, `sources[]` | V3 |

**Rules**

- JSON Schemas for every model are served at `/v1/schemas` and exported to `schemas/` in the repo.
- `schema_version` follows semver; breaking changes bump the major and keep the old version for one release.
- Optional fields are explicitly nullable, never silently omitted.
- Enums over free strings wherever the set is known.
- Callers can pass their own JSON Schema or a registered model name for extraction (V2).

---

## 5. V1: Reliable search and read, fully observable

V1 gives an agent dependable multi-engine search, high-quality page reading and a live view of everything the service is doing. **It makes no model calls; AI features start in V2.**

### Search

- **V1-01 Search API.** `POST /v1/search` queries SearXNG's JSON API and returns a `SearchResponse`. Results from all engines are merged, deduplicated by canonical URL, and scored by rank across engines and the number of engines that agree.
- **V1-02 Search intents.** `intent` maps to a SearXNG engine and category preset: `general`, `technical` (GitHub, Stack Overflow, MDN, docs sites), `library`, `product`, `standard`, `news`, `academic`. Presets live in a config file, not code.
- **V1-03 Engine resilience.** Failed or rate-limited engines are recorded in `unresponsive_engines` and logged as events. If too few results come back, retry once with a broader preset.

### Read

- **V1-04 Fetch API.** `POST /v1/fetch` returns a `Document`. Tiered pipeline: static fetch with httpx and Trafilatura first; escalate to Crawl4AI's browser when the page is JS-rendered, the body is thin (configurable word threshold) or the static fetch fails.
- **V1-05 Structured page data.** Every `Document` includes tables parsed from HTML as `Table` objects, embedded JSON-LD, microdata and OpenGraph via extruct, and outbound links with anchor text.
- **V1-06 Basic PDFs.** PDFs are detected by content type and converted to text with pypdf. Full document intelligence arrives in V2.
- **V1-07 Batch fetch.** `POST /v1/fetch/batch` takes up to 50 URLs and runs as a job.

### Jobs and orchestration

- **V1-08 Search and read.** `POST /v1/search_read` searches, then fetches the top N results with bounded concurrency, returning documents ranked by search score. It runs as a job.
- **V1-09 Job model.** Long work returns a `Job` id at once. `GET /v1/jobs/{id}` returns status and progress; `?wait=60` blocks until done or timeout. Jobs can be cancelled. An in-process asyncio worker backed by a SQLite queue is enough for V1.
- **V1-10 Cache.** SQLite cache keyed on normalised query or URL, with separate TTLs for searches and pages, and a per-request bypass flag. Cache hits are flagged in `meta`.
- **V1-11 Politeness.** robots.txt checked with Protego, per-domain concurrency and delay limits, a configurable User-Agent, and a maximum response size.

### Agent integration

- **V1-12 MCP server.** Tools `web_search`, `web_fetch`, `search_and_read` and `get_job`, each declaring an output schema and returning structured content, served over streamable HTTP.
- **V1-13 Typed Python client.** A small async client in the same uv workspace that returns the shared Pydantic models, for use from any agent framework.
- **V1-14 OpenAPI.** FastAPI's OpenAPI spec published at `/openapi.json`, with examples for each endpoint.

### GUI and observability

- **V1-15 Event log.** Every step emits an `Event` to SQLite and an in-memory pub/sub. JSON logs also go to stdout via structlog.
- **V1-16 Activity dashboard.** One page served by FastAPI with HTMX and server-sent events:
  - A "Now" panel showing running jobs, their progress, the current URL or engine in use, and queue depth.
  - A live, auto-scrolling event log with filters by level, job, event kind and free text, plus pause.
  - A health strip for SearXNG, Crawl4AI and the cache (up or down, hit rate, jobs today).
- **V1-17 Job detail view.** Click a job to see its request, a timeline of its events, and its results as a collapsible JSON viewer plus rendered markdown for each document.
- **V1-21 Test console.** A "Try it" page in the GUI that exercises the service without writing code:
  - Forms for Search, Fetch and Search-and-read, each with a **Run** button that calls the same public REST endpoints an agent uses, with no private shortcuts.
  - A dropdown of demo queries and URLs that show the service off (e.g. "compare open-source vector databases", a JS-heavy docs page, a vendor PDF with tables).
  - Results shown three ways: readable cards (title, source, engines that agreed, quality), rendered markdown with extracted tables and JSON-LD, and the raw JSON envelope.
  - The run's events stream into the activity log live, so you see each engine query and page fetch as it happens.
  - A "Copy as" button giving the equivalent curl command, Python client call and MCP tool call, so a demo turns straight into agent code.
  - Past test runs listed with a one-click re-run.
- **V1-18 Health and config.** `GET /health` checks dependencies; `GET /v1/engines` lists engines and presets. Config via environment variables and pydantic-settings, with a `.env.example`.

### Project and deployment

- **V1-19 uv project.** Python 3.12+, `src/` layout, a uv workspace with `research_engine` (service) and `research_engine_client` (client and shared models), `uv.lock` committed. Tooling: ruff, pyright, pytest, pytest-asyncio and respx.
- **V1-20 Docker Compose.** Services: `app`, `searxng` (with the JSON format enabled, a curated engine list and the limiter off for internal use) and `crawl4ai`. One self-contained Compose project, running on the `toolbox` VM behind the shared Caddy, built to coexist with other tools there, and operated through scripted ops commands (sections 9 and 10). Optional services (Gluetun VPN profiles, Ollama) sit behind Compose profiles so they start only when enabled. SQLite data lives on a named volume, the browser container gets a larger shared-memory size (`shm_size` ~1 GB), and all services restart automatically.
- **V1-22 Ops tooling.** The `ops/` scripts, `Makefile`, `CLAUDE.md` and `docs/OPERATIONS.md` described in section 10 are part of V1.

### V1 acceptance criteria

- [ ] A typical technical query returns at least 10 deduplicated results drawn from at least 3 engines.
- [ ] Fetch returns clean markdown for a static article, a JS-rendered single-page app and a PDF.
- [ ] Every response validates against its published JSON Schema in tests.
- [ ] A running `search_read` job's events appear in the GUI within 1 second.
- [ ] The whole stack starts with `docker compose up` and needs no API keys or paid services.
- [ ] An agent built with the OpenAI Agents SDK can use the MCP tools end to end.
- [ ] From the test console, a non-developer can run each demo query and see results, extracted structure and the live event trail without touching code.

---

## 6. V2: Deeper reading and structured extraction

V2 turns pages into typed facts: schema-driven extraction, full document parsing, better ranking and free specialist sources that matter for software research.

### Models

- **V2-00 Model selector.** One gateway (LiteLLM) for Anthropic, OpenAI, Google and local Ollama models. Each AI task (extraction, query expansion, claim extraction, planning, verification) gets its own default model, set in config and from a dropdown in the GUI. Any request can override the model with a `model` parameter. API keys come from environment variables only.
- **V2-00a Cost tracking.** Every model call records tokens, cost and latency as a `ModelCall` event, rolled up per job, per session and per month. A configurable monthly budget stops model calls when reached, and the GUI shows spend against it.

### Extraction

- **V2-01 Schema-driven extraction.** `POST /v1/extract` takes URLs or document ids plus a JSON Schema or a registered model name, and returns `Extraction[T]`. Uses Instructor with the model the selector assigns to extraction; validation failures trigger up to 3 repair retries. Every field carries the quote and character span it came from.
- **V2-02 Rule-based extraction.** For repeatable page layouts, a CSS or XPath schema (Crawl4AI's JSON CSS strategy) extracts without any LLM. Saved per domain and reused.
- **V2-03 Schema registry.** Named, versioned schemas stored in the service, e.g. `LibraryProfile`, `ProductFeatureMatrix`, `PricingTier`, `ApiEndpoint`, `CompetitorSummary`. Listed at `/v1/schemas/registry`.
- **V2-04 Claim extraction.** Breaks a document into atomic `Claim` objects with evidence quotes, so the calling agent reasons over facts rather than prose.

### Documents

- **V2-05 Document intelligence.** Docling replaces pypdf for PDF, DOCX, PPTX and complex HTML, giving a heading tree, sections and properly structured tables.
- **V2-06 Passage chunking.** Documents split into `Passage` objects along the heading tree, each with its heading path and character span.
- **V2-07 Site crawl.** `POST /v1/crawl` performs a bounded crawl of a docs site: sitemap first, then links, with depth, page limit and include or exclude patterns. Results form a corpus tied to the job.

### Search quality

- **V2-08 Query expansion.** Generates query variants (synonyms, `site:` targeting of official docs, recency filters) with rules plus an optional LLM, runs them in parallel and fuses results with reciprocal rank fusion.
- **V2-09 Reranking.** Local cross-encoder reranks passages against the research question and returns the top passages with scores.
- **V2-10 Source assessment.** Each source gets a `SourceAssessment`: domain class from a maintained list (official docs, standards bodies, vendors, community, news), freshness and content quality signals.

### Specialist sources (free APIs)

- **V2-11 Connectors.** Typed connectors for GitHub (repo, README, stars, licence, latest release, last commit), PyPI and npm (versions, dependencies, licence), Stack Exchange, arXiv, Wikipedia and Wikidata, Hacker News via Algolia, and OSV.dev for known vulnerabilities. GitHub uses a free personal token for higher rate limits.
- **V2-12 Archive fallback.** When a page is gone or blocked, try the Wayback Machine and mark `method: archive` in provenance.

### Network egress

- **V2-19 Egress profiles.** Outbound traffic can leave through named profiles: `direct` (home connection, the default) or one or more VPN exits run as Gluetun containers, each pinned to a country (e.g. `vpn-uk`, `vpn-us`, `vpn-de`). Each Gluetun container exposes its built-in HTTP proxy, so the app picks a profile per request rather than routing whole containers through the VPN.
  - `egress` is an optional field on search, fetch and job requests; SearXNG and Crawl4AI receive the matching proxy.
  - Uses: region-specific results (local pricing, availability, regional vendors) and keeping research traffic off the home IP.
  - Fallback: when the direct route is rate-limited, retry once through a VPN profile, recorded in provenance as the egress used.
  - Not used to get around a site's explicit blocks, logins or CAPTCHAs.
  - Gluetun needs the TUN device and `NET_ADMIN` capability in its Compose service; both are available as standard in the VM.
  - The GUI health strip shows each profile's status and exit country; the test console has an egress dropdown.
  - Needs a VPN provider subscription supported by Gluetun (the one item outside the no-paid-infrastructure rule; optional).

### Sessions and integration

- **V2-13 Research sessions.** Jobs, documents and claims group under a `session_id` that can carry the requirement's JIRA key, so all research for one requirement is retrievable in one call.
- **V2-14 Webhooks.** Optional callback URL on any job, called with the result envelope on completion.
- **V2-15 Untrusted-content flags.** Fetched text is scanned for hidden text and instruction-like patterns aimed at AI agents; hits are added to `Document.warnings` rather than silently passed on.

### GUI additions

- **V2-16 Session view.** All jobs, sources and claims for a session, with source assessments.
- **V2-17 Extraction inspector.** Side by side: the source page with highlighted evidence spans, and the extracted JSON with validation status.
- **V2-18 Re-run and compare.** Re-run any job and diff the new result against the old one.
- Model selector dropdowns, spend-vs-budget panel, and egress dropdown in the test console.

### V2 acceptance criteria

- [ ] Extracting a `LibraryProfile` from a project's docs and GitHub repo gives schema-valid output with evidence for every populated field.
- [ ] Tables in a sample vendor PDF come back as `Table` objects with correct headers and rows.
- [ ] Reranked passages beat raw search order on a small hand-labelled set of 20 questions.
- [ ] One call returns everything gathered for a given session or JIRA key.
- [ ] Switching the extraction model in the GUI changes the model used on the next call, and its cost appears in the spend panel.

---

## 7. V3: Autonomous research partner with memory

V3 runs whole research briefs end to end, verifies what it finds, remembers past research and measures its own quality.

### Deep research

- **V3-01 Research brief API.** `POST /v1/research` takes a brief (question, requirement context, output schema, budget in steps and minutes) and returns a `ResearchReport`. A planner breaks the brief into sub-questions, then a loop of search, read, extract and gap-check runs until coverage criteria or the budget are met.
- **V3-02 Pluggable reasoning model.** The planner and verifier use the models the selector assigns to those tasks, overridable per request. The primitive endpoints from V1 and V2 stay available for agents that prefer to drive the loop themselves.
- **V3-03 Plan tree.** Sub-questions, the queries run for each, and which sources answered them are stored as a tree and returned with the report.

### Verification

- **V3-04 Corroboration.** Each claim is matched against claims from other sources; the report gives a corroboration count per claim and prefers independent domains.
- **V3-05 Conflict detection.** Contradicting claims (different values, versions or dates for the same thing) are grouped into `conflicts[]` with all evidence, for the calling agent or a human to resolve.
- **V3-06 Confidence scoring.** Report and claim confidence combine source assessment, corroboration and freshness, with the reasons listed.

### Memory

- **V3-07 Research memory.** Passages and claims are embedded and stored in LanceDB. Before searching the web, the service checks "have we researched this before?" and reuses fresh results.
- **V3-08 Entity graph.** Libraries, products, vendors, standards and organisations become `Entity` and `Relation` records (e.g. *library implements standard*, *product competes with product*), queryable and exportable as JSON.
- **V3-09 Watches.** Scheduled re-checks of chosen sources (a library's releases, a pricing page, a spec) with diffs emitted as events and optional webhooks.

### Harder sources

- **V3-10 Hardened browsing.** A Camoufox browser profile, retries with backoff, and archive fallback for sites that block plain headless browsers. CAPTCHA solving stays out of scope.
- **V3-11 Visual capture.** Page screenshots and OCR of images and diagrams via Docling, attached to documents as evidence.

### Quality and observability

- **V3-12 Evaluation harness.** A golden set of research briefs with expected key sources and facts. Nightly runs measure source recall, fact accuracy and schema validity, and flag regressions.
- **V3-13 Tracing.** OpenTelemetry spans for every step of a research job.
- **V3-14 Plugins.** New connectors, extractors and search providers register through Python entry points without changes to core code.

### GUI additions

- **V3-15 Research run view.** The live plan tree with each sub-question's status, a waterfall timeline of steps, and the report alongside its evidence.
- **V3-16 Quality dashboard.** Evaluation scores over time and the latest regressions.
- **V3-17 Knowledge explorer.** Browse and search the entity graph and research memory.

### V3 acceptance criteria

- [ ] A brief such as "compare three open-source auth libraries for a FastAPI app" returns a schema-valid report with corroborated claims and any conflicts listed.
- [ ] A repeat brief within the cache window reuses memory and makes measurably fewer web requests.
- [ ] The evaluation harness runs unattended and shows trends in the GUI.

---

## 8. Non-functional requirements

- **Security.** Bind to the lab network only. Require an API key header on REST and MCP. Block fetches to private and loopback IP ranges (SSRF protection) unless a host is explicitly allow-listed. Enforce response size limits and a content-type allow-list. Model API keys from environment only, never logged.
- **Reliability.** Every adapter has timeouts, retries with backoff and typed errors. A failed engine or page never fails the whole job; jobs end as `partial` with errors listed.
- **Timeouts.** Generous defaults that favour quality: 60 seconds per page, 15 minutes per job. All configurable.
- **Async throughout.** httpx and asyncio; no blocking calls on the event loop.
- **Testing.** Unit tests with mocked engines (respx) and recorded page fixtures (static article, JS app, PDF, page with JSON-LD and tables). Integration tests against the live Compose stack, marked so they can be skipped.
- **Resources.** This stack's own budget: about 4 vCPU and 8 GB RAM, mostly for the headless browser, enforced with per-service limits because the VM is shared (section 9). With API models selected, V2 and V3 need no GPU. If Ollama is run locally later, give it its own VM or LXC (with GPU passthrough if available) to keep this service light.
- **Licensing.** AGPL services run in their own containers and are called over HTTP only. The project itself is released under the **MIT licence** (`LICENSE` file in the repo root); it is compatible with every component used because none is linked into AGPL code.
- **Public repository.** The repo is public and shared, so:
  - **No secrets, ever.** `.env`, data, backups, `deploys.jsonl`, logs and any credentials are git-ignored from the very first commit; only `.env.example` with placeholder values is committed.
  - **Secret scanning.** A `pre-commit` hook runs [gitleaks](https://github.com/gitleaks/gitleaks) on every commit, and GitHub secret scanning with push protection is turned on for the repo.
  - **No lab-specific values in code.** Hostnames, IPs, paths, usernames and ports come from `.env` with neutral defaults (e.g. `SITE_HOST=research.localhost`). The `toolbox` set-up is documented as one example deployment in `docs/deploy-toolbox.md`, not hard-wired.
  - **Written for outsiders.** `README.md` explains what the project is, a quick start with `docker compose up`, configuration, and how to use the API, MCP tools and GUI. Internal notes (this requirements file, ADRs, `CLAUDE.md`) can stay public but must contain nothing sensitive.
  - **Standard project files:** `LICENSE`, `README.md`, `CONTRIBUTING.md` (short), `SECURITY.md` (how to report a vulnerability), and a CI workflow (GitHub Actions) running ruff, pyright, pytest and gitleaks on every push and pull request.
  - **Dependabot** (or Renovate) enabled for Python dependencies, GitHub Actions and pinned Docker images.

---

## 9. Environments and deployment

### One environment

There is a single environment: the `toolbox` VM. Development and production happen in the same place. Claude Code runs on `toolbox` in the repo checkout at `/opt/research-engine/`, and the stack running from that checkout is the live service. There is no separate dev or staging stack.

This keeps things simple, so the safety net comes from process rather than separate environments:

- Changes are committed to git before they go live, so every running version is a known commit.
- `make deploy` rebuilds and restarts the stack from the current commit, runs the smoke test, and rolls back to the last good commit automatically if it fails (section 10).
- Tests (unit tests with mocked engines) run with `uv run pytest` without touching the live stack.

### Host: the `toolbox` VM

- **Role:** `toolbox` is the owner's VM for self-hosted tools, used both to develop them and to run them. This service is the first tenant; other tools will follow, so the stack must be self-contained and must not assume it owns the machine.
- **OS:** Debian, with Docker Engine and the Compose plugin (shared by all tools).
- **Why a VM, not an LXC:** the service loads untrusted web pages in a headless browser. A VM gives it its own kernel (stronger isolation from the Proxmox host), lets Chromium's sandbox run normally (no `--no-sandbox`), and avoids Docker-in-LXC nesting, AppArmor and TUN-passthrough workarounds. Other tools on the VM share a kernel with this stack, so they are isolated from it at container level only; keep untrusted-content handling inside the hardened `crawl4ai` container.
- **Sizing:** start at 8 vCPU and 16 GB RAM (this stack's own budget is about 4 vCPU / 8 GB). Spare capacity is available on the Proxmox host, so grow the VM as tools are added rather than squeezing limits.
- **Network:** fixed IP via a DHCP reservation on the UniFi Dream Router.
- **QEMU guest agent:** enabled in Proxmox (VM → Options → QEMU Guest Agent → Use QEMU Guest Agent) and installed in the guest:
  ```bash
  sudo apt update && sudo apt install qemu-guest-agent
  sudo systemctl start qemu-guest-agent
  ```
  The "static unit" message on Debian is normal; it starts automatically at boot. If the Proxmox option was just enabled, do a full Shutdown + Start from Proxmox. Verify via the VM's Summary page (IPs shown) or `qm agent <vmid> ping` on the host.
- **Snapshots:** take a Proxmox snapshot before VM-level changes (OS, Docker upgrades). A snapshot covers every tool on the VM, so rolling one back affects all tenants; per-tool rollback uses git tags (section 10).

### Co-tenancy rules (this stack must follow these)

- **Own directory:** everything lives under `/opt/research-engine/` (the git checkout Claude Code works in, `.env`, backups). Each future tool gets its own `/opt/<tool>/`.
- **Own Compose project name:** set `name: research-engine` in `compose.yaml` so containers, networks and volumes are prefixed and never collide with another tool's. No hard-coded `container_name`.
- **Minimal published ports:** nothing in this stack publishes a host port except an optional debug port on `app` (`APP_PORT` in `.env`, off by default). `searxng`, `crawl4ai`, `gluetun-*` and `ollama` never publish ports; they talk over the project's private network only.
- **Reverse proxy:** a single shared Caddy serves every tool on the VM by hostname. Caddy runs as its own small Compose project (`/opt/caddy/`) and owns ports 80/443. It creates an external Docker network `proxy`; this stack's `app` service joins both its private network and `proxy`, and nothing else in the stack joins `proxy`.
- **Resource limits:** every service sets memory and CPU limits (`deploy.resources.limits` or `mem_limit`/`cpus`), and `shm_size` (~1 GB) is set on the browser service only, so a runaway browser cannot starve other tools. Limits configurable via `.env`.
- **Named volumes only, prefixed by the project name;** no writes outside `/opt/research-engine/` and Docker volumes.
- **No privileged containers and no Docker socket mounts.** `NET_ADMIN` and `/dev/net/tun` are granted only to the Gluetun containers.
- **Container hardening** for `crawl4ai` and `app`: run as non-root, `no-new-privileges`, drop unneeded capabilities, read-only root filesystem where the image allows.
- **Logs:** Docker log rotation set per service (`max-size`, `max-file`) so one tool cannot fill the shared disk.
- **Lifecycle independence:** `docker compose up/down/pull` in `/opt/research-engine/` must never affect other tools. Never use `docker system prune -a` or other VM-wide clean-ups in scripts.

### Compose layout

- One project named `research-engine`: `app`, `searxng`, `crawl4ai` always on; `gluetun-*` and `ollama` behind Compose profiles (`--profile vpn`, `--profile ollama`).
- Every image pinned to an explicit version tag (never `latest`); upgrades happen by changing the pin in git.
- A `healthcheck` on every service; `restart: unless-stopped` everywhere; named volume for SQLite; resource limits and log rotation on every service.

### Shared Caddy (shipped in this repo, installed once)

There is no reverse proxy elsewhere in the lab yet, so this repo provides the VM's shared Caddy as a separate, tool-agnostic project:

- `deploy/caddy/compose.yaml`: one `caddy` service (official image), project name `caddy`, publishes 80 and 443, creates the external network `proxy`, named volumes for Caddy data and config, `restart: unless-stopped`, log rotation.
- `deploy/caddy/Caddyfile`: a global section plus `import sites/*.caddy`, so each tool drops in its own site file without editing the main Caddyfile.
- `deploy/caddy/sites/research-engine.caddy`: the site block for this service, proxying `research.toolbox` (set by `SITE_HOST` in `.env`) to `app:8000` over the `proxy` network. SSE for the GUI must work through the proxy (no response buffering on the event stream).
- **TLS:** default to Caddy's `tls internal` (local CA) for lab hostnames, with a README note on trusting Caddy's root certificate on the MacBook. Leave a commented alternative for plain HTTP.
- **Hostnames:** the VM is `toolbox`; each tool on it gets a subdomain, starting with `research.toolbox` for this service. Hitting plain `toolbox` serves a small Caddy index page linking to each tool's subdomain (a static page in `deploy/caddy/`, extended as tools are added).
- **DNS (UniFi Dream Router):** local DNS records live in UniFi Network under Settings → Policy Table → Create New Policy → DNS (Network 9.4; in 9.3 it's Settings → Policy Engine → DNS). UniFi doesn't document wildcard records, so use:
  - one **Host (A)** record: `toolbox` → the VM's fixed IP;
  - one **Alias (CNAME)** record per tool: `research.toolbox` → `toolbox`.
  Adding a tool later means one new CNAME plus a Caddy site file; if the VM's IP ever changes, only the A record changes. Give the VM a fixed IP via a DHCP reservation on the router. The README documents these steps.
- **Install:** copy `deploy/caddy/` to `/opt/caddy/` on the VM and run `docker compose up -d` there **before** starting this stack. Future tools add a file to `/opt/caddy/sites/` and run `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.
- Optionally restrict the GUI to the lab subnet in the site block (`remote_ip` matcher).

---

## 10. Operations: Claude as DevOps operator

The owner uses Claude Code on `toolbox` both to build the service and to operate it. Everything an operator does is therefore a **scripted, idempotent, non-interactive command with machine-readable output**, so Claude (or the owner) can run it, check the result and decide the next step without guesswork.

### Ops commands

Scripts live in `ops/` and are wrapped by a `Makefile` (or `justfile`). Each prints a one-line JSON summary as its final output and uses exit codes (0 success, non-zero failure).

| Command | What it does |
| --- | --- |
| `make bootstrap` | One-off, idempotent VM setup: Docker check, `/opt/research-engine` and `/opt/caddy` layout, `proxy` network, shared Caddy install, backup timer. Needs sudo; the owner runs or approves it. |
| `make deploy` | Refuse if the working tree has uncommitted changes; build the app image from the current commit, pull pinned images, `docker compose up -d --wait`, run the smoke test; on failure, roll back automatically to the last good commit and report |
| `make rollback` | Check out and redeploy the last good commit from `deploys.jsonl` (on a branch, so no work is lost) |
| `make status` | Container states, restart counts, health, per-container CPU/memory (`docker stats --no-stream`, this project only), volume disk usage, running commit and any tag, last deploy, last backup. Secrets redacted. |
| `make health` | Calls `/health` and each container healthcheck; JSON result |
| `make smoke` | Runs the test-console demo set against the live API (search, fetch, search-and-read); under 2 minutes; JSON pass/fail per check |
| `make logs SERVICE=app SINCE=30m` | Recent logs for one service, JSON lines |
| `make backup` | Online SQLite backup to `/opt/research-engine/backups/`, with retention (default 14 days) |
| `make restore FILE=…` | Stop `app`, restore the backup, start `app`, run `health` |
| `make version` | Running commit, any tag, and image versions |

### Release and deploy rules

- The live stack always runs a **committed** version; `make deploy` refuses a dirty working tree. Git tags (semver, e.g. `v1.0.0`) mark milestones and are pushed to GitHub, but day-to-day deploys can be any commit.
- Every deploy and rollback is appended to `/opt/research-engine/deploys.jsonl` (time, from-commit, to-commit, result, smoke outcome); the last successful entry is the rollback target.
- `/health` returns JSON with each dependency's state plus the running version and git SHA; `/version` returns the same identity alone.
- Nightly backup runs from a systemd timer on the VM installed by `make bootstrap`; `status` reports the last backup's time and size.

### Access and guardrails

- Claude Code is installed on `toolbox` and runs there as the `admin` user, which is in the `sudo` and `docker` groups and owns each `/opt/<tool>/` folder. Membership of the `docker` group is effectively root on that VM; accepted for the home lab and recorded as an ADR.
- Editing code, running tests and **read-only** commands (`status`, `health`, `logs`, `version`, `smoke`) may run freely. Commands that **restart or restore the live service** (`deploy`, `rollback`, `restore`, `bootstrap`) require the owner's approval each time; configure Claude Code's permissions so they prompt.
- Never run `docker compose down -v`, delete volumes or backups, or force-push, without the owner explicitly asking.
- Secrets live only in `/opt/research-engine/.env` (mode 600, git-ignored), never committed, never printed by scripts or logs.
- Operations never touch other tools' directories under `/opt/`, other Compose projects, or VM-wide Docker state, apart from adding this tool's site file to the shared Caddy and reloading it.
- The repo includes `CLAUDE.md` (how to build, test and operate this project, including these guardrails) and `docs/OPERATIONS.md` (the human runbook: commands, deploy, rollback, backup and restore, incident checklist).

### Ops acceptance criteria

- [ ] From a fresh Debian VM, following the README plus `make bootstrap` and `make deploy` gives a working stack at `https://research.toolbox`.
- [ ] A deliberately broken commit fails its smoke test on `make deploy` and rolls back automatically, with the event recorded in `deploys.jsonl`.
- [ ] A backup can be restored with `make restore` and the service passes `make health` afterwards.
- [ ] Every ops command exits non-zero on failure and ends with a JSON summary Claude can parse.

---

## 11. Build order for V1

0. Public-repo hygiene: `.gitignore`, `.env.example`, `LICENSE` (MIT), gitleaks pre-commit hook, CI workflow, `README.md` skeleton.
1. Models package and JSON Schema export.
2. SearXNG adapter and `/v1/search`.
3. Fetch pipeline: static path, browser escalation, extruct, tables.
4. SQLite store, job runner, cache and event bus.
5. `/v1/search_read` and batch fetch.
6. GUI: activity dashboard, then job detail, then the test console.
7. MCP server and typed client.
8. Docker Compose (following the co-tenancy rules in section 9), SearXNG `settings.yml`, and the shared Caddy project in `deploy/caddy/` with this tool's site file.
9. Ops scripts and `Makefile` (section 10), `CLAUDE.md`, `docs/OPERATIONS.md`, and a deployment README covering VM setup, Caddy install, DNS and TLS trust.

## 12. Instructions for Claude Code

- Check the current versions and APIs of SearXNG, Crawl4AI, Trafilatura, extruct and the MCP Python SDK before writing adapters; do not rely on remembered APIs.
- Use `uv` for everything (`uv init`, `uv add`, `uv run`, `uv sync --frozen` in the Dockerfile).
- Record notable decisions as short ADRs in `docs/adr/`; seed them from the decisions log in section 2.
- Keep every external tool behind its adapter interface.
- Do not add any model/LLM calls in V1.
- Treat this document's feature IDs (V1-01 and so on) as JIRA ticket and commit references.
- The repo is public: set up `.gitignore`, `.env.example`, the gitleaks pre-commit hook and `LICENSE` before anything else, and never commit secrets or lab-specific values.
- Never configure Chromium with `--no-sandbox`.
- `toolbox` is where you develop and also the live host, shared with other tools: follow the co-tenancy rules in section 9 and never write anything that assumes this stack owns the host (fixed host ports, global Docker clean-ups, writes outside `/opt/research-engine/`).
- Edit and test freely in the checkout, but put changes live only by committing and running `make deploy`; use only the ops commands in section 10 to operate the stack, and ask before any command that restarts or restores it.

## 13. Open questions

- Project name: "Research Engine" is a placeholder.
- In V3, should the service run its own planning loop, or should research planning stay entirely in the calling agent?
- Should research sessions key on JIRA issue keys or the factory's own requirement ids?
- Which VPN provider (if any) for the V2 egress profiles?
