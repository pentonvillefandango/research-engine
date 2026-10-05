# Research Engine

A self-hosted web search and scraping service for research agents. Agents call it over REST or MCP. It returns typed, provenance-tagged data: ranked search results, and pages as clean markdown with their tables and structured data.

**Building an agent that uses it? Start with the [agent usage guide](docs/USING.md).**

Research quality comes before speed. The reasoning stays in the calling agent: this service finds, fetches, cleans and structures, and makes no AI model calls of its own.

## Status

V1.0.1 (search and read, fully observable) is released: tag `v1.0.1` on `main` (patch fixes on top of `v1.0.0`). V2 and V3 are planned in [REQUIREMENTS.md](REQUIREMENTS.md).

## Features (V1)

- **Meta-search** through [SearXNG](https://github.com/searxng/searxng): several engines at once, deduplicated and ranked, with intent presets (`general`, `technical`, `library`, `product`, `standard`, `news`, `academic`).
- **Tiered fetching:** a fast static fetch first, escalating to a sandboxed headless Chromium ([Crawl4AI](https://github.com/unclecode/crawl4ai)) for JavaScript pages, thin pages and bot walls.
- **Clean output:** markdown, tables, links, JSON-LD, microdata and OpenGraph, and PDFs as markdown. Every document carries provenance (final URL, fetch time, `sha256:` content hash).
- **Background jobs** for batches and search-and-read, with progress and long-polling.
- **Safety and politeness:** SSRF protection, robots.txt, per-domain pacing, size and content-type limits.
- **Interfaces:** REST, MCP (four tools), a typed async Python client, and published JSON Schemas.
- **Live GUI:** a dashboard with a live event stream, job pages and a test console.
- **Operations:** scripted deploy with smoke test and automatic rollback, backups and health checks (see [docs/OPERATIONS.md](docs/OPERATIONS.md)).

## Quick start

You need Docker with the Compose plugin, and `openssl`. The commands are for Linux; on macOS, write `sed -i ''` instead of `sed -i`. This starts a local development stack. It needs no reverse proxy, and it runs as its own Compose project (`research-engine-dev`), so it can never replace a live `research-engine` stack on the same host.

```bash
git clone https://github.com/pentonvillefandango/research-engine.git
cd research-engine
cp .env.example .env
for k in API_KEY SESSION_SECRET SEARXNG_SECRET CRAWL4AI_API_TOKEN; do
  sed -i "s/^$k=.*/$k=$(openssl rand -hex 32)/" .env
done
sed -i 's/^APP_PORT=.*/APP_PORT=8765/' .env   # any free local port
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml up -d --build --wait
```

Open `http://127.0.0.1:8765/` and log in with the `API_KEY` from `.env`. The app is published on `127.0.0.1` only. Stop the stack with:

```bash
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml down
```

Don't add `-v` unless you want to delete the dev stack's data. For a real deployment behind Caddy with HTTPS, see [Deployment](#deployment).

## Configuration

All settings come from `.env` (copy `.env.example`; never commit `.env`). Secrets are marked. Generate each secret with `openssl rand -hex 32`. Each must be at least 32 characters and not the `.env.example` placeholder: the app refuses to start otherwise (naming the variable, never its value), and `make deploy` and `make health` check all four, `SEARXNG_SECRET` included.

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_KEY` | none (secret) | The key for REST, MCP and the GUI login, sent as `X-API-Key`. |
| `SESSION_SECRET` | none (secret) | Signs GUI session cookies. |
| `SITE_HOST` | `research.localhost` | The public host name Caddy serves. Must equal `SITE_HOST` in Caddy's own `.env`, or `/mcp` returns 421. |
| `GIT_SHA` | empty | Leave empty. `make deploy` and `make rollback` set it per run; it tags the app image and shows in `/health`. |
| `LOG_LEVEL` | `INFO` | App log level. |
| `USER_AGENT` | `ResearchEngine/1.0 (+https://github.com/...)` | Sent with every fetch. Its first word is the robots.txt user-agent token. |
| `SEARXNG_URL` | `http://searxng:8080` | SearXNG URL, for local (non-Compose) runs only. |
| `CRAWL4AI_URL` | `http://crawl4ai:11235` | Crawl4AI URL, for local runs only. |
| `CRAWL4AI_API_TOKEN` | none (secret) | Token between the app and Crawl4AI. |
| `SEARXNG_SECRET` | none (secret) | SearXNG's secret key (Compose only; never given to the app). |
| `INTENTS_FILE` | `config/intents.yaml` | Intent presets: engines and categories per intent. |
| `DEMOS_FILE` | `config/demos.yaml` | Demo requests for the test console and `make smoke`. |
| `SEARCH_MIN_RESULTS` | `10` | If a non-general intent finds fewer results, the search is retried with the general engines. |
| `SEARCH_TIMEOUT_S` | `30` | Timeout for one search. |
| `PAGE_TIMEOUT_S` | `60` | Upper bound on any single page fetch, in seconds: each fetch (REST, MCP and job pages) runs for `min(timeout_s, PAGE_TIMEOUT_S)`. The robots.txt check runs inside that budget, so below 15 s (the robots.txt timeout) a slow robots.txt is cut off every time: nothing is cached and each fetch to that site ends in `upstream_timeout`. |
| `THIN_WORD_THRESHOLD` | `150` | Pages with fewer words count as thin and, in `auto` mode, escalate to the browser. |
| `MAX_RESPONSE_BYTES` | `10485760` | Largest page accepted (10 MiB). |
| `ALLOWED_CONTENT_TYPES` | HTML, XHTML, PDF, plain text | Content types the fetcher accepts. |
| `SSRF_ALLOW_HOSTS` | empty | Comma-separated host names exempt from the SSRF block. |
| `DOMAIN_CONCURRENCY` | `2` | Most requests in flight to one site. |
| `DOMAIN_DELAY_S` | `1.0` | Least gap between requests to one site (robots.txt `Crawl-delay` can raise it, up to 30 s). |
| `CACHE_TTL_SEARCH_S` | `3600` | Search cache lifetime. |
| `CACHE_TTL_PAGE_S` | `86400` | Page cache lifetime. |
| `DB_PATH` | `data/research-engine.sqlite` | SQLite file for local runs. Compose fixes it to the `app-data` volume. |
| `JOB_WORKERS` | `2` | Jobs run at the same time. |
| `JOB_FETCH_CONCURRENCY` | `5` | Pages fetched at the same time within one job. |
| `JOB_TIMEOUT_S` | `900` | A job is stopped after this long. |
| `EVENT_RETENTION_DAYS` | `30` | Events older than this are deleted. |
| `APP_PORT` | empty | Optional debug port: with `compose.debug.yaml`, the app is published on `127.0.0.1:<APP_PORT>` only. |
| `APP_MEM_LIMIT` | `1g` | Memory limit for `app`. |
| `APP_CPUS` | `1.0` | CPU limit for `app`. |
| `SEARXNG_MEM_LIMIT` | `512m` | Memory limit for `searxng`. |
| `SEARXNG_CPUS` | `1.0` | CPU limit for `searxng`. |
| `CRAWL4AI_MEM_LIMIT` | `4g` | Memory limit for `crawl4ai`. |
| `CRAWL4AI_CPUS` | `2.0` | CPU limit for `crawl4ai`. |

## API usage

Send `X-API-Key` on every `/v1` request. The examples use `BASE=https://research.localhost` and `KEY` holding your API key. For the quick-start stack, use `BASE=http://127.0.0.1:8765` (and MCP at `http://127.0.0.1:8765/mcp`). The full OpenAPI description is at `/openapi.json` (there is no `/docs` page, on purpose: it would load third-party JavaScript on the GUI's origin).

```bash
H=(-H "X-API-Key: $KEY" -H 'Content-Type: application/json')

# search
curl -s "${H[@]}" "$BASE/v1/search" -d '{"query": "sqlite wal mode", "intent": "technical", "max_results": 10}'
# read one page (mode: auto | static | browser)
curl -s "${H[@]}" "$BASE/v1/fetch" -d '{"url": "https://www.rfc-editor.org/rfc/rfc9110.html", "mode": "auto"}'
# read up to 50 pages as a job (202 + Location header)
curl -s "${H[@]}" "$BASE/v1/fetch/batch" -d '{"urls": ["https://example.com/", "https://example.org/"]}'
# search, then read the top 5, as a job
curl -s "${H[@]}" "$BASE/v1/search_read" -d '{"search": {"query": "sqlite wal mode"}, "top_n": 5}'
# get a job, waiting up to 30 s for it to finish (long-poll; 0-60)
curl -s "${H[@]}" "$BASE/v1/jobs/<job-id>?wait=30"
# cancel a job
curl -s "${H[@]}" -X DELETE "$BASE/v1/jobs/<job-id>"
# engines and intent presets; JSON Schemas
curl -s "${H[@]}" "$BASE/v1/engines"
curl -s "${H[@]}" "$BASE/v1/schemas"
curl -s "${H[@]}" "$BASE/v1/schemas/Document"
# no key needed
curl -s "$BASE/health"
curl -s "$BASE/version"
```

**Envelope.** Every response (except a raw schema) has this shape:

```json
{
  "data": { "...": "the result" },
  "meta": { "request_id": "…", "schema_version": "1.0.0", "took_ms": 812, "cache_hit": false },
  "errors": []
}
```

**Errors.** On failure `data` is null and `errors` lists `{code, message, retryable, source}`. Codes include `invalid_request` (422), `unauthorized` (401), `not_found` (404), `ssrf_blocked` and `robots_disallowed` (403), `response_too_large` (413), `content_type_not_allowed` (415), `fetch_failed`, `upstream_error` and `extraction_failed` (502), `upstream_timeout` (504) and `internal_error` (500). `retryable` says whether trying again later may help.

**Jobs.** `/v1/fetch/batch` and `/v1/search_read` return `202` with the queued job. Poll `GET /v1/jobs/{id}?wait=N`: it returns as soon as the job is finished, or after `N` seconds (at most 60) with its progress. A job ends `done`, `partial` (some pages failed; see `result.failed`), `failed` or `cancelled`.

## MCP

The MCP server is at `https://<SITE_HOST>/mcp` (streamable HTTP, stateless, JSON responses). Send the API key in the `X-API-Key` header; the GUI session cookie is not accepted here. Only POST is served. `/mcp/` works the same way; neither path redirects. It has four tools, each with an output schema and structured content: `web_search`, `web_fetch`, `search_and_read` and `get_job`. The server rejects a `Host` header outside `SITE_HOST`, `localhost`, `127.0.0.1` and `research.localhost`.

```json
{
  "mcpServers": {
    "research-engine": {
      "type": "http",
      "url": "https://research.localhost/mcp",
      "headers": { "X-API-Key": "${RESEARCH_ENGINE_API_KEY}" }
    }
  }
}
```

Tool arguments, agent patterns and a system-prompt snippet are in the [agent usage guide](docs/USING.md). [examples/openai_agents_mcp.py](examples/openai_agents_mcp.py) is a working OpenAI Agents SDK agent.

## Python client

`research-engine-client` is a typed async client with the shared Pydantic models. Install it from this repository:

```bash
pip install "git+https://github.com/pentonvillefandango/research-engine@v1.0.1#subdirectory=packages/research_engine_client"
```

```python
from research_engine_client import ResearchEngineClient
from research_engine_client.models import SearchRequest

async with ResearchEngineClient("https://research.localhost", api_key=key) as client:
    found = await client.search(SearchRequest(query="sqlite wal mode"))
```

It has `search`, `fetch`, `fetch_batch`, `search_read`, `get_job`, `wait_for_job`, `cancel_job`, `engines`, `health` and `version`. Failures raise `ResearchEngineError` with the typed errors and `.retryable`. See [examples/client_usage.py](examples/client_usage.py). The examples' README explains [trusting Caddy's CA](examples/README.md#tls-trusting-caddys-internal-ca).

## GUI

Open `https://<SITE_HOST>/` and log in with the API key. The session lasts 12 hours.

- **Dashboard** (`/`): service health, running and recent jobs, and a live event stream with filters.
- **Job pages** (`/jobs/<id>`): the request, progress, a timeline and the results.
- **Test console** (`/try`): run searches, fetches and jobs against the real API, including the demo set from `config/demos.yaml`. It shows the rendered results, the raw JSON, the live events and past runs, and builds a copyable `curl` command (without the key).

## Security notes

- **API key** on every `/v1` and `/mcp` request; the GUI uses a signed, HTTP-only session cookie with a same-origin check.
- **SSRF:** fetches to loopback, private, link-local, CGNAT and other internal addresses are blocked (ADR-0026). `SSRF_ALLOW_HOSTS` exempts named hosts. Static fetches are checked on every redirect hop. Browser fetches are not: the start URL and the final URL are checked, but the redirects Chromium follows in between and the page's subresources (images, scripts, frames, XHR) are not checked by this service, only by Crawl4AI's own internal-URL block. Blocking the browser container's LAN egress at the network layer is planned for V2 (ADR-0022).
- **robots.txt** is obeyed, with per-domain pacing and `Crawl-delay`.
- **Browser sandbox:** Chromium runs with its sandbox on, never `--no-sandbox` (ADR-0022).
- **Untrusted content:** fetched pages are data, not instructions. The GUI renders them safely, and agents should treat them the same way (see the [usage guide](docs/USING.md#fetched-content-is-untrusted)).
- Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Deployment

The stack runs as one Docker Compose project behind a shared Caddy reverse proxy that serves HTTPS with its own internal CA. Only Caddy publishes ports, and it admits only the lab subnet.

- [docs/deploy-toolbox.md](docs/deploy-toolbox.md) is a worked example: a Debian VM named `toolbox`, serving the service at `https://research.toolbox.home.arpa`.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) is the runbook: `make deploy`, rollback, backups and incidents.
- [deploy/caddy/README.md](deploy/caddy/README.md) covers the shared Caddy.

## Development

```bash
uv sync
uv run pre-commit install        # gitleaks and file hygiene on every commit
make test                        # ruff, ruff format --check, pyright, pytest
```

Unit tests mock every upstream and never touch a running stack. Integration tests run against the dev stack, in its own Compose project:

```bash
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml up -d --wait searxng crawl4ai
source <(scripts/dev_urls.sh)    # exports SEARXNG_LIVE_URL and CRAWL4AI_LIVE_URL
uv run pytest -m integration
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml down
```

`uv run research-engine schemas export --check` checks that `schemas/` is current (CI runs it). `tests/service/unit/test_response_schemas.py` calls every public endpoint and validates each real response, errors included, against its file in `schemas/`. See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## Licence

MIT. See [LICENSE](LICENSE).
