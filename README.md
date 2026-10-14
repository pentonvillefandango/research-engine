# Research Engine

Research Engine is a self-hosted web search and reading service for AI research agents. An agent asks it a question ("search for X", "read this page", "search for X and read the top five results") and gets back tidy, structured results it can rely on. It returns a ranked list of search results, and each web page is converted into clean markdown with its tables and other structured data kept, plus a record of where and when the page was fetched.

**Building an agent that uses it? Start with the [agent usage guide](docs/USING.md).**

The thinking stays in your agent. This service only finds, fetches, cleans and organises information. It makes no AI model calls of its own, and it puts research quality ahead of speed.

## How it works

```
 your agent or app                          the web
        │  REST or MCP, with an API key        ▲
        ▼                                      │
 ┌──────────────┐   ┌──────────────────────────┴─────────────┐
 │ Caddy (HTTPS)│──▶│ Research Engine app                    │
 └──────────────┘   │  • search  ──▶ SearXNG (many engines)  │
                    │  • read    ──▶ quick static fetch, or  │
                    │               Crawl4AI's sandboxed     │
                    │               Chrome for JS-heavy pages│
                    │  • clean, cache, log, run jobs         │
                    └────────────────────────────────────────┘
```

- **Caddy** is the front door. It serves HTTPS and only lets in machines on your own network.
- **The app** (Python, FastAPI) does the real work: it checks the API key, runs searches, fetches and cleans pages, caches results, runs longer jobs in the background and serves the web interface.
- **[SearXNG](https://github.com/searxng/searxng)** queries several search engines at once. The app merges, de-duplicates and ranks what comes back.
- **[Crawl4AI](https://github.com/unclecode/crawl4ai)** runs a headless Chrome, with Chrome's security sandbox switched on, for pages that need JavaScript or that a plain download can't read properly.
- **SQLite** stores jobs, events, the cache, API keys and the request log in one file.

All of this runs as one Docker Compose project.

## What it can do

- **Search well.** Several engines are queried at once, then the results are merged, de-duplicated and ranked. Presets tune the search for what you're after: `general`, `technical`, `library`, `product`, `standard`, `news` or `academic`.
- **Read almost any page.** It tries a quick, plain download first and switches to the real browser only when it has to: for JavaScript pages, pages with too little text, or bot walls.
- **Return clean content.** Pages come back as markdown, along with their tables, links and embedded structured data (JSON-LD, microdata, OpenGraph). PDFs come back as markdown too. Every page records its final URL, when it was fetched and a fingerprint of its content (`sha256:` hash).
- **Run big jobs in the background.** Read up to 50 pages at once, or search and then read the top results. You can watch progress and wait for the result.
- **Be a good citizen.** It obeys robots.txt, spaces out requests to each site, refuses oversized or unexpected files, and won't fetch addresses inside your network (SSRF protection).
- **Track who uses it.** Give each app its own API key, see how much each one uses the service, and switch any key off instantly.
- **Show you what's happening.** A web interface has a live dashboard, job pages, a key-management page and a test console.
- **Fit any client.** It offers a REST API, an MCP server (four tools), a typed Python client and published JSON Schemas.
- **Be easy to run.** One-command deploys with a smoke test and automatic rollback, plus nightly backups and health checks (see [docs/OPERATIONS.md](docs/OPERATIONS.md)).

## Status

**v1.1.0** is released and tagged on `main`. It is V1 plus the optional GUI password and per-app API keys with a usage log. V2 (sending traffic out through a VPN, and optional local AI models) and V3 are planned in [REQUIREMENTS.md](REQUIREMENTS.md).

## Quick start: try it on your own machine

You need Docker with the Compose plugin, and `openssl`. The commands are for Linux; on macOS, write `sed -i ''` instead of `sed -i`.

This starts a private test copy on your machine, with no HTTPS proxy. It runs as its own Compose project (`research-engine-dev`), so it can never replace a real `research-engine` stack on the same host.

```bash
git clone https://github.com/pentonvillefandango/research-engine.git
cd research-engine
cp .env.example .env
# fill in the four secrets with random values
for k in API_KEY SESSION_SECRET SEARXNG_SECRET CRAWL4AI_API_TOKEN; do
  sed -i "s/^$k=.*/$k=$(openssl rand -hex 32)/" .env
done
sed -i 's/^APP_PORT=.*/APP_PORT=8765/' .env   # any free local port
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml up -d --build --wait
```

Then open `http://127.0.0.1:8765/` and log in with the `API_KEY` value from `.env`. The app listens on `127.0.0.1` only, so nothing outside your machine can reach it. Try the **Test console** to run a search.

To stop it:

```bash
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml down
```

Don't add `-v` unless you also want to delete the test copy's data. To run it properly behind Caddy with HTTPS, see [Running it for real](#running-it-for-real-deployment).

## Signing in and API keys

There are two kinds of credential: one for people using the web interface and one for apps calling the API.

### Signing in to the web interface

By default you sign in with the `API_KEY` from `.env`. You can switch to a normal password instead:

```bash
uv run research-engine gui set-password   # asks twice; at least 12 characters
make deploy
```

The command stores only a scrambled form of the password (a scrypt hash, `GUI_PASSWORD_HASH`) in `.env`, never the password itself. Once it's set, the login page asks only for the password; the API key no longer opens the interface. To go back to key login, delete the `GUI_PASSWORD_HASH` line from `.env` and run `make deploy`. A sign-in lasts 12 hours, and repeated wrong attempts are locked out for a short while.

### Giving each app its own key

If several apps share one key, you can't tell them apart or cut one off without breaking the others. So give each app its own key:

1. Sign in to the web interface and open **Keys**.
2. Type a name, such as `newsdesk` or `laptop-agent`, and press **Create**.
3. Copy the key (`re_…`) straight away. It's shown **once only**, because the service keeps just a fingerprint of it (a SHA-256 hash). If you lose a key, revoke it and create a new one.
4. Put the key in the app's settings. The app sends it in the `X-API-Key` header, the same way as before.

**Revoke** a key on the same page and it stops working immediately. Revoked keys stay listed, greyed out, so their history remains readable.

Only someone signed in to the web interface can manage keys. A request that carries an API key is refused on the Keys page, so a leaked app key can't be used to create more keys.

### The built-in key

The `API_KEY` in `.env` keeps working. It appears on the Keys page as **env (built-in)** and can't be revoked there. The operations scripts (`make smoke`, `make health`) use it. Once your apps have their own keys you can change it; see [Rotating secrets](docs/OPERATIONS.md#rotating-secrets).

### The usage log

Every API call (`/v1` and `/mcp`) is recorded with:
- when it started;
- which key or caller made it;
- the method and path;
- the MCP tool name, if any;
- the result status;
- how long it took;
- the caller's IP address.

The Keys page shows, for each key:
- calls in the last 24 hours and the last 30 days;
- when it was last used, and from which IP.

It also has a list of recent calls that you can filter by key or narrow to errors only.

The log never stores search terms, request bodies or the pages fetched. Entries are deleted after 30 days (`REQUEST_LOG_RETENTION_DAYS`). Calls made from the web interface's test console show as **GUI session**. More detail is in the [runbook](docs/OPERATIONS.md#gui-password-and-api-keys) and ADR-0028.

## Using it from an app

### REST

Send your key in the `X-API-Key` header on every `/v1` request. These examples use `BASE=https://research.localhost` and `KEY` holding your key. For the quick-start copy, use `BASE=http://127.0.0.1:8765`. The full machine-readable API description is at `/openapi.json`. There is deliberately no `/docs` page, because it would load third-party JavaScript next to the web interface.

```bash
H=(-H "X-API-Key: $KEY" -H 'Content-Type: application/json')

# search
curl -s "${H[@]}" "$BASE/v1/search" -d '{"query": "sqlite wal mode", "intent": "technical", "max_results": 10}'
# read one page (mode: auto | static | browser)
curl -s "${H[@]}" "$BASE/v1/fetch" -d '{"url": "https://www.rfc-editor.org/rfc/rfc9110.html", "mode": "auto"}'
# read up to 50 pages as a background job (202 + Location header)
curl -s "${H[@]}" "$BASE/v1/fetch/batch" -d '{"urls": ["https://example.com/", "https://example.org/"]}'
# search, then read the top 5, as a background job
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

**Every answer has the same shape.** Every response, except a raw schema, is wrapped like this:

```json
{
  "data": { "...": "the result" },
  "meta": { "request_id": "…", "schema_version": "1.0.0", "took_ms": 812, "cache_hit": false },
  "errors": []
}
```

**When something goes wrong,** `data` is null and `errors` lists `{code, message, retryable, source}`. `retryable` tells your agent whether trying again later might help. The codes:

| Code | HTTP status |
| --- | --- |
| `invalid_request` | 422 |
| `unauthorized` | 401 |
| `not_found` | 404 |
| `ssrf_blocked`, `robots_disallowed` | 403 |
| `response_too_large` | 413 |
| `content_type_not_allowed` | 415 |
| `fetch_failed`, `upstream_error`, `extraction_failed` | 502 |
| `upstream_timeout` | 504 |
| `internal_error` | 500 |

**Background jobs.** `/v1/fetch/batch` and `/v1/search_read` answer straight away with `202` and a job. Ask for `GET /v1/jobs/{id}?wait=N`: it replies as soon as the job finishes, or after `N` seconds (at most 60) with progress so far. A job ends as one of:
- `done`;
- `partial`: some pages failed, listed in `result.failed`;
- `failed`;
- `cancelled`.

### MCP (for AI agents)

The MCP server is at `https://<SITE_HOST>/mcp`, using streamable HTTP, stateless, with JSON responses.
- **Key:** send it in the `X-API-Key` header. The web interface's sign-in cookie doesn't work here.
- **Paths and method:** only POST is served, and `/mcp/` works too.
- **Tools:** `web_search`, `web_fetch`, `search_and_read` and `get_job`, each with an output schema.
- **Host check:** the server refuses any `Host` header other than `SITE_HOST`, `localhost`, `127.0.0.1` and `research.localhost`.

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

Tool arguments, good agent patterns and a ready-made system-prompt snippet are in the [agent usage guide](docs/USING.md). [examples/openai_agents_mcp.py](examples/openai_agents_mcp.py) is a working OpenAI Agents SDK agent.

### Python client

`research-engine-client` is a typed async client that shares the service's data models. Install it from this repository:

```bash
pip install "git+https://github.com/pentonvillefandango/research-engine@v1.1.0#subdirectory=packages/research_engine_client"
```

```python
from research_engine_client import ResearchEngineClient
from research_engine_client.models import SearchRequest

async with ResearchEngineClient("https://research.localhost", api_key=key) as client:
    found = await client.search(SearchRequest(query="sqlite wal mode"))
```

- **Methods:** `search`, `fetch`, `fetch_batch`, `search_read`, `get_job`, `wait_for_job`, `cancel_job`, `engines`, `health` and `version`.
- **Errors:** failures raise `ResearchEngineError`, which carries the typed errors and `.retryable`.
- **More:** see [examples/client_usage.py](examples/client_usage.py). The examples' README explains [how to trust Caddy's certificate](examples/README.md#tls-trusting-caddys-internal-ca).

## The web interface

Open `https://<SITE_HOST>/` and sign in (see [Signing in and API keys](#signing-in-and-api-keys)).

- **Dashboard** (`/`): whether the service and its parts are healthy, running and recent jobs, and a live feed of events you can filter.
- **Job pages** (`/jobs/<id>`): what was asked, progress, a timeline and the results.
- **Keys** (`/keys`): create and revoke a key per app, see how much each key is used, and browse recent API calls.
- **Test console** (`/try`): run real searches, page reads and jobs, including the ready-made demos in `config/demos.yaml`. It shows:
  - the formatted results;
  - the raw JSON;
  - the live events;
  - your past runs;
  - a `curl` command you can copy, without your key.

## Security, in plain words

- **Nothing works without a key.** Every API call needs a valid key: the built-in `.env` key or a per-app key from the Keys page. Keys are stored only as fingerprints (ADR-0028). The web interface uses a signed, browser-only sign-in cookie, and checks that form posts come from its own pages.
- **It won't be used to poke around your network.** Fetches to loopback, private, link-local, CGNAT and other internal addresses are blocked (ADR-0026), and `SSRF_ALLOW_HOSTS` can exempt named hosts.
  - **Quick downloads:** checked on every redirect.
  - **Browser fetches:** the starting and final addresses are checked.
  - **Browser gap:** redirects inside the browser, and the images and scripts a page loads, rely on Crawl4AI's own internal-address block. Blocking the browser's access to your local network is planned for V2 (ADR-0022).
- **It's polite.** It obeys robots.txt, paces requests per site and respects `Crawl-delay`.
- **The browser is sandboxed.** Chrome runs with its security sandbox on, never with `--no-sandbox` (ADR-0022).
- **Fetched pages are treated as data, not instructions.** The interface displays them safely, and agents should do the same (see the [usage guide](docs/USING.md#fetched-content-is-untrusted)).
- **Found a vulnerability?** Report it as described in [SECURITY.md](SECURITY.md).

## Running it for real (deployment)

For real use, the stack runs as one Docker Compose project behind a shared Caddy reverse proxy. Caddy serves HTTPS using its own internal certificate authority. Only Caddy opens network ports, and it only lets in your lab network.

- [docs/deploy-toolbox.md](docs/deploy-toolbox.md) is a worked example: a Debian VM called `toolbox`, serving the service at `https://research.toolbox.home.arpa`.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) is the runbook. It covers:
  - deploying with `make deploy`, and rolling back;
  - backups;
  - the GUI password and API keys;
  - rotating secrets;
  - what to do when something breaks.
- [deploy/caddy/README.md](deploy/caddy/README.md) covers the shared Caddy.

Changes go live only by committing them and running `make deploy`. It builds from the committed code, runs a smoke test and rolls back automatically if anything fails.

## Configuration reference

Every setting comes from `.env`. Copy `.env.example` to make it, and never commit `.env`.

**Secrets:**
- **Generating them:** make each with `openssl rand -hex 32`.
- **What the app checks:** each must be at least 32 characters and must not be the `.env.example` placeholder. Otherwise the app refuses to start, naming the setting but never showing its value.
- **What the scripts check:** `make deploy` and `make health` check all four secrets, `SEARXNG_SECRET` included.

| Variable | Default | What it does |
| --- | --- | --- |
| `API_KEY` | none (secret) | The built-in API key. It works for REST and MCP (sent as `X-API-Key`), and for signing in to the web interface when no password is set. Shown as "env (built-in)" on the Keys page. |
| `GUI_PASSWORD_HASH` | empty | Optional scrambled (scrypt) form of the web interface password, set by `research-engine gui set-password`. When it's empty, sign-in uses `API_KEY`. |
| `SESSION_SECRET` | none (secret) | Signs the web interface's sign-in cookies. |
| `SITE_HOST` | `research.localhost` | The public host name Caddy serves. Must match `SITE_HOST` in Caddy's own `.env`, or `/mcp` answers 421. |
| `GIT_SHA` | empty | Leave empty. `make deploy` and `make rollback` fill it in each time; it labels the app image and shows in `/health`. |
| `LOG_LEVEL` | `INFO` | How much the app logs. |
| `USER_AGENT` | `ResearchEngine/1.0 (+https://github.com/...)` | Sent with every fetch. Its first word is the name robots.txt rules are matched against. |
| `SEARXNG_URL` | `http://searxng:8080` | Where SearXNG is. Only needed when running the app outside Compose. |
| `CRAWL4AI_URL` | `http://crawl4ai:11235` | Where Crawl4AI is. Only needed when running the app outside Compose. |
| `CRAWL4AI_API_TOKEN` | none (secret) | Shared token between the app and Crawl4AI. |
| `SEARXNG_SECRET` | none (secret) | SearXNG's own secret key (Compose only; never given to the app). |
| `INTENTS_FILE` | `config/intents.yaml` | The search presets: which engines and categories each one uses. |
| `DEMOS_FILE` | `config/demos.yaml` | Ready-made demo requests for the test console and `make smoke`. |
| `SEARCH_MIN_RESULTS` | `10` | If a specialised preset finds fewer results than this, the search is retried with the general engines. |
| `SEARCH_TIMEOUT_S` | `30` | Time limit for one search, in seconds. |
| `PAGE_TIMEOUT_S` | `60` | The most time any single page read may take, in seconds: each read runs for `min(timeout_s, PAGE_TIMEOUT_S)`. The robots.txt check counts towards this, so below 15 s (the robots.txt time limit) a slow robots.txt is always cut off: nothing is cached and every read from that site ends in `upstream_timeout`. |
| `THIN_WORD_THRESHOLD` | `150` | Pages with fewer words than this count as "thin" and, in `auto` mode, are re-read with the browser. |
| `MAX_RESPONSE_BYTES` | `10485760` | Largest page accepted (10 MiB). |
| `ALLOWED_CONTENT_TYPES` | HTML, XHTML, PDF, plain text | Which kinds of file the fetcher accepts. |
| `SSRF_ALLOW_HOSTS` | empty | Comma-separated host names that may be fetched even though they look internal. |
| `DOMAIN_CONCURRENCY` | `2` | Most requests in flight to one site at a time. |
| `DOMAIN_DELAY_S` | `1.0` | Shortest gap between requests to one site, in seconds. A site's robots.txt `Crawl-delay` can raise it, up to 30 s. |
| `CACHE_TTL_SEARCH_S` | `3600` | How long search results are cached, in seconds. |
| `CACHE_TTL_PAGE_S` | `86400` | How long fetched pages are cached, in seconds. |
| `DB_PATH` | `data/research-engine.sqlite` | SQLite file for runs outside Compose. Compose fixes it to the `app-data` volume. |
| `JOB_WORKERS` | `2` | How many background jobs run at the same time. |
| `JOB_FETCH_CONCURRENCY` | `5` | How many pages one job reads at the same time. |
| `JOB_TIMEOUT_S` | `900` | A job is stopped after this many seconds. |
| `EVENT_RETENTION_DAYS` | `30` | Events older than this are deleted. |
| `REQUEST_LOG_RETENTION_DAYS` | `30` | How many days of the per-key API usage log to keep. |
| `APP_PORT` | empty | Optional debug port: with `compose.debug.yaml`, the app is also reachable on `127.0.0.1:<APP_PORT>` only. |
| `APP_MEM_LIMIT` | `1g` | Memory limit for the app. |
| `APP_CPUS` | `1.0` | CPU limit for the app. |
| `SEARXNG_MEM_LIMIT` | `512m` | Memory limit for SearXNG. |
| `SEARXNG_CPUS` | `1.0` | CPU limit for SearXNG. |
| `CRAWL4AI_MEM_LIMIT` | `4g` | Memory limit for Crawl4AI. |
| `CRAWL4AI_CPUS` | `2.0` | CPU limit for Crawl4AI. |

## Development

```bash
uv sync
uv run pre-commit install        # secret scanning and file tidiness on every commit
make test                        # lint, format check, type check and all unit tests
```

Unit tests stand in for every outside service, so they never touch a running stack. Integration tests run against the test copy, in its own Compose project:

```bash
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml up -d --wait searxng crawl4ai
source <(scripts/dev_urls.sh)    # exports SEARXNG_LIVE_URL and CRAWL4AI_LIVE_URL
uv run pytest -m integration
docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml down
```

`uv run research-engine schemas export --check` confirms the published schemas in `schemas/` are up to date; CI runs it too. `tests/service/unit/test_response_schemas.py` calls every public endpoint and checks each real response, errors included, against its schema. Conventions are in [CONTRIBUTING.md](CONTRIBUTING.md), and design decisions are recorded in [docs/adr/](docs/adr/).

## Licence

MIT. See [LICENSE](LICENSE).
