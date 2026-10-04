# Step 6: GUI. Activity dashboard, job detail, test console

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** V1-16 (activity dashboard), V1-17 (job detail), V1-21 (test console), B2 (session-cookie auth).

**Outcome:** A server-rendered GUI built with Jinja2, vendored htmx 2.0.11 with htmx-ext-sse 2.2.4, and FastAPI-native SSE. It has:
- a login page;
- a live dashboard: a "Now" panel, an event log with filters and pause, and a health strip;
- a job detail view;
- a "Try it" console that calls the public REST API.

**GUI security baseline (every task):**
- **Content Security Policy:** `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`.
  - This means no inline `<script>`, no inline `style=` and no `hx-on`.
  - htmx is configured with `<meta name="htmx-config" content='{"includeIndicatorStyles":false,"allowEval":false,"selfRequestsOnly":true}'>`.
- **Untrusted content:** all page content from the web goes through `render_untrusted_markdown()`, which uses markdown-it-py and then the nh3 allow-list. It is never marked `|safe` in any other way. Jinja autoescape stays on.
- **Session cookie:**
  - Cookie-authenticated state-changing requests (POST, PUT, PATCH, DELETE) must carry an `Origin` header (or, failing that, a `Referer`) whose host equals `SITE_HOST` or the request `Host`. Otherwise they get 403. This is CSRF defence in depth on top of `SameSite=Strict`.
  - API-key requests are exempt from this check.

**Task order:** 6.1, then 6.2, then 6.3, then 6.4. They run serially because they share `app.py`, the templates and `gui/routes.py`.

---

### Task 6.1: Session auth, base layout, vendored assets, sanitiser

**Goal:** Add the login and logout flow with a signed HttpOnly cookie that the auth middleware accepts alongside `X-API-Key`. Add the base layout with CSP, the vendored htmx assets (integrity-checked), and the untrusted-markdown renderer.

**Files:**
- Modify: `packages/research_engine/pyproject.toml` (deps: `jinja2`, `itsdangerous`, `python-multipart`, `markdown-it-py`, `nh3`)
- Create: `packages/research_engine/src/research_engine/gui/{__init__,session,render,routes}.py`
- Create: `packages/research_engine/src/research_engine/gui/templates/{base,login}.html`
- Create: `packages/research_engine/src/research_engine/gui/static/{app.css,app.js}`
- Create: `packages/research_engine/src/research_engine/gui/static/vendor/{htmx.min.js,sse.js}` and `VENDOR.md` (versions and SRI hashes)
- Create: `scripts/vendor_assets.sh`
- Modify: `packages/research_engine/src/research_engine/api/auth.py` (cookie path, GUI redirect, Origin check)
- Modify: `packages/research_engine/src/research_engine/app.py` (mount `/static`, GUI router, security headers middleware)
- Test: `tests/service/unit/test_gui_auth.py`, `tests/service/unit/test_render.py`

**Acceptance Criteria:**
- [ ] `GET /login` returns 200 with the form and the CSP header. `POST /login` with the right key:
  - sets a cookie `re_session` (HttpOnly, `SameSite=Strict`, `Path=/`, `Max-Age=43200`, and `Secure` when the request scheme is https);
  - redirects 303 to `next`, which must be a same-site relative path that starts with `/` and not `//`; anything else falls back to `/`.
- [ ] A wrong key returns 401 and re-renders the form with "Invalid key". After 5 failures from one client IP within 60 s, further attempts get 429 for 60 s.
- [ ] The cookie value is an `itsdangerous.TimestampSigner(session_secret)` signature over `"gui"`. Tampered or expired cookies (older than 12 h) are rejected.
- [ ] Unauthenticated `GET /` redirects 303 to `/login?next=/`. Unauthenticated `/v1/*` still returns a 401 envelope; it is never redirected.
- [ ] A valid cookie authenticates `/v1/*`, so the test console works. A cookie-authenticated `POST /v1/search` with a foreign `Origin` returns 403. The same request with an `X-API-Key` and a foreign Origin returns 200.
- [ ] `POST /logout` clears the cookie and redirects to `/login`.
- [ ] `render_untrusted_markdown` strips `<script>`, `onerror=`, `javascript:` links, `<img>`, `<iframe>` and `style` attributes. It keeps headings, lists, tables, code and `<a href="https://...">`, with `rel="noopener noreferrer nofollow"` added.
- [ ] Vendored files match the npm registry's `dist.integrity` for `htmx.org@2.0.11` and `htmx-ext-sse@2.2.4`. `scripts/vendor_assets.sh --check` exits 0.

**Verify:** `uv run pytest tests/service/unit/test_gui_auth.py tests/service/unit/test_render.py -v && scripts/vendor_assets.sh --check` → all pass, `vendor ok`

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_gui_auth.py
import httpx


def anon(app) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://research.localhost")


async def _login(c: httpx.AsyncClient, key: str = "test-key") -> httpx.Response:
    return await c.post("/login", data={"api_key": key, "next": "/"}, headers={"Origin": "http://research.localhost"})


async def test_redirects_to_login(app) -> None:  # noqa: ANN001
    async with anon(app) as c:
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
        assert (await c.post("/v1/search", json={"query": "x"})).status_code == 401


async def test_login_sets_cookie_and_authenticates_api(app) -> None:  # noqa: ANN001
    async with anon(app) as c:
        r = await _login(c)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie
        ok = await c.post("/v1/search", json={"query": "x", "depth": "quick"},
                          headers={"Origin": "http://research.localhost"})
        assert ok.status_code == 200
        bad = await c.post("/v1/search", json={"query": "x"}, headers={"Origin": "http://evil.example"})
        assert bad.status_code == 403


async def test_open_redirect_blocked(app) -> None:  # noqa: ANN001
    async with anon(app) as c:
        r = await c.post("/login", data={"api_key": "test-key", "next": "//evil.example/"},
                         headers={"Origin": "http://research.localhost"})
        assert r.headers["location"] == "/"


async def test_bad_key_and_rate_limit(app) -> None:  # noqa: ANN001
    async with anon(app) as c:
        codes = [(await _login(c, "wrong")).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


async def test_tampered_cookie_rejected(app) -> None:  # noqa: ANN001
    async with anon(app) as c:
        c.cookies.set("re_session", "gui.forged.sig")
        assert (await c.get("/")).status_code == 303


async def test_api_key_ignores_origin(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/search", json={"query": "x", "depth": "quick"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


async def test_csp_header(app) -> None:  # noqa: ANN001
    async with anon(app) as c:
        r = await c.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
```

```python
# tests/service/unit/test_render.py
from research_engine.gui.render import render_untrusted_markdown


def test_strips_dangerous() -> None:
    out = render_untrusted_markdown(
        "# Hi\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n<img src=x onerror=alert(1)>\n\n"
        '<iframe src="https://e.example"></iframe>\n\n<p style="color:red">p</p>')
    low = out.lower()
    for bad in ("<script", "javascript:", "onerror", "<img", "<iframe", "style="):
        assert bad not in low
    assert "<h1>hi</h1>" in low


def test_keeps_safe_markup() -> None:
    out = render_untrusted_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n\n- item\n\n`code`\n\n[ok](https://ok.example)")
    assert "<table>" in out and "<li>item</li>" in out and "<code>code</code>" in out
    assert 'href="https://ok.example"' in out and "noopener" in out
```

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Vendor the assets.** `scripts/vendor_assets.sh` downloads `https://registry.npmjs.org/<pkg>/-/<name>-<ver>.tgz` for each package, verifies the tarball against `dist.integrity` (sha512, base64) from `https://registry.npmjs.org/<pkg>/<ver>`, extracts `package/dist/htmx.min.js` and the sse extension file (find its path with `tar tzf`), and writes them to `gui/static/vendor/`. The `--check` flag re-downloads, compares bytes, and prints `vendor ok`. `VENDOR.md` records the versions, source URLs, integrity values and the date.

```bash
#!/usr/bin/env bash
# Vendor htmx + SSE extension from the npm registry with integrity verification. Usage: [--check]
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=packages/research_engine/src/research_engine/gui/static/vendor
mkdir -p "$DEST"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fetch() {  # pkg version member outname
  local pkg=$1 ver=$2 member=$3 out=$4 meta tarball integrity actual
  meta=$(curl -fsS "https://registry.npmjs.org/$pkg/$ver")
  tarball=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["dist"]["tarball"])' <<<"$meta")
  integrity=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["dist"]["integrity"])' <<<"$meta")
  curl -fsS "$tarball" -o "$tmp/$out.tgz"
  actual="sha512-$(openssl dgst -sha512 -binary "$tmp/$out.tgz" | base64 -w0)"
  [ "$actual" = "$integrity" ] || { echo "integrity mismatch for $pkg@$ver" >&2; exit 1; }
  tar -xzf "$tmp/$out.tgz" -C "$tmp" "package/$member"
  if [ "${CHECK:-0}" = 1 ]; then cmp -s "$tmp/package/$member" "$DEST/$out" || { echo "drift: $out" >&2; exit 1; }
  else cp "$tmp/package/$member" "$DEST/$out"; fi
  rm -rf "$tmp/package"
}
[ "${1:-}" = "--check" ] && CHECK=1
fetch htmx.org 2.0.11 dist/htmx.min.js htmx.min.js
fetch htmx-ext-sse 2.2.4 dist/sse.min.js sse.js
echo "vendor ok"
```

If `dist/sse.min.js` isn't the right member path, find the real one with `tar tzf` and fix it. Run the script and commit the vendored files.

- [ ] **Step 4: Implement `gui/session.py`**

```python
"""Signed GUI session cookie (B2). Value: TimestampSigner(session_secret).sign('gui')."""

import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

COOKIE = "re_session"
MAX_AGE_S = 12 * 3600


class SessionCodec:
    def __init__(self, secret: str) -> None:
        self._signer = TimestampSigner(secret, salt="research-engine-gui")

    def issue(self) -> str:
        return self._signer.sign(b"gui").decode()

    def valid(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            return self._signer.unsign(value, max_age=MAX_AGE_S) == b"gui"
        except (BadSignature, SignatureExpired):
            return False


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, window_s: float = 60.0) -> None:
        self._max, self._window = max_failures, window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, client: str) -> bool:
        q = self._fails[client]
        now = time.monotonic()
        while q and q[0] < now - self._window:
            q.popleft()
        return len(q) >= self._max

    def fail(self, client: str) -> None:
        self._fails[client].append(time.monotonic())

    def reset(self, client: str) -> None:
        self._fails.pop(client, None)
```

- [ ] **Step 5: Extend `api/auth.py`.** The middleware now takes `api_key`, `codec: SessionCodec` and `site_host`. The order of checks is:
  1. Open path: pass through.
  2. Valid `X-API-Key`: pass through.
  3. Valid `re_session` cookie: for state-changing methods, verify that the `Origin` (or `Referer`) host is in `{site_host, request Host}`, else return 403 with an envelope `unauthorized` and the message "cross-origin request refused". Then pass through.
  4. Otherwise: for paths starting `/v1/` or `/mcp`, return the 401 envelope. For any other path, return 303 to `/login?next=<path>`.

  Parse cookies with `http.cookies.SimpleCookie` from the raw header.

- [ ] **Step 6: Implement `gui/render.py`**

```python
"""Render untrusted fetched content safely (§1 'treat all fetched content as untrusted')."""

import nh3
from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_TAGS = {"a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "dl", "dt", "em", "h1", "h2", "h3", "h4",
         "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "strong", "sub", "sup", "table", "tbody", "td",
         "th", "thead", "tr", "ul"}


def render_untrusted_markdown(text: str) -> str:
    html = _MD.render(text)
    return nh3.clean(html, tags=_TAGS, attributes={"a": {"href", "title"}, "th": {"align"}, "td": {"align"}},
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow")
```

`"html": False` makes markdown-it escape raw HTML. nh3 is the second layer.

- [ ] **Step 7: Templates and routes.**
  - `base.html` holds:
    - the htmx-config meta;
    - `<link rel="stylesheet" href="/static/app.css">`;
    - `<script src="/static/vendor/htmx.min.js" defer>`, `<script src="/static/vendor/sse.js" defer>` and `<script src="/static/app.js" defer>`;
    - a nav with Dashboard, Try it and a Logout form (POST);
    - `{% block content %}`.
  - `login.html` holds:
    - a form POSTing to `/login` with `api_key` (password input), a hidden `next`, and an error slot;
    - the text "Use the API key from your .env (API_KEY)".
  - `gui/routes.py` has `GET /login`, `POST /login` and `POST /logout`, using `Jinja2Templates(directory=Path(__file__).parent / "templates")`.
  - A `SecurityHeadersMiddleware` (pure ASGI, in `gui/routes.py`) adds the CSP, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `X-Frame-Options: DENY` to every response that isn't under `/v1` or `/mcp`.
  - In `app.py`:
    - mount `StaticFiles(directory=gui_dir / "static")` at `/static`;
    - include the GUI router;
    - create `SessionCodec(settings.session_secret.get_secret_value())` and pass it to the middleware;
    - store `LoginRateLimiter` on `app.state`.
  - `app.css`: plain, readable, responsive CSS with a monospace event log and colour-coded levels.

- [ ] **Step 8: Run the tests.** Expected: PASS.

- [ ] **Step 9: Commit.** `git commit -m "feat(gui): session-cookie login, CSP layout, vendored htmx and safe markdown rendering (V1-16, B2)"`

---

### Task 6.2: Live event stream and activity dashboard

**Goal:** `GET /gui/events/stream` streams filtered history and then live events as SSE HTML fragments. `GET /` shows the "Now" panel, the live event log (filters, pause, auto-scroll) and the health strip.

**Files:**
- Modify: `packages/research_engine/src/research_engine/gui/routes.py`
- Create: `packages/research_engine/src/research_engine/gui/templates/{dashboard,_event_row,_now,_health}.html`
- Modify: `packages/research_engine/src/research_engine/gui/static/app.js` (pause, auto-scroll, filter, reconnect)
- Modify: `packages/research_engine/src/research_engine/jobs/store.py` (add `count_since(dt)`)
- Test: `tests/service/unit/test_gui_dashboard.py`

**Acceptance Criteria:**
- [ ] `GET /gui/events/stream?level=&job=&kind=&q=` returns `text/event-stream` (FastAPI `EventSourceResponse`) with:
  - first, up to 200 matching historical events (`bus.tail` filtered), each as an SSE `event: log` whose data is the rendered `_event_row.html`;
  - then live events matching the same filters;
  - a heartbeat comment every 15 s;
  - the header `X-Accel-Buffering: no`.
- [ ] **Within 1 second** of `Emitter.info(...)` being awaited, the matching row arrives on an open stream. A test asserts this with a 1.0 s timeout.
- [ ] Events that don't match the filters aren't sent. The filters are level (minimum level), job id, kind prefix and case-insensitive text.
- [ ] `GET /gui/partials/now` renders the running jobs (id, type, progress `done/total`, current URL or engine), the queue depth (`runner.queue_depth`) and the 10 most recent jobs, each linking to `/jobs/{id}`. The dashboard refreshes it with `hx-trigger="load, every 2s, sse:log"` on the same SSE connection (wrapped in the `hx-ext="sse"` container), so job changes show within about 1 s.
- [ ] `GET /gui/partials/health` renders the `/health` dependencies as pills (up, down or degraded), the cache hit rate, and jobs today (`job_store.count_since(midnight UTC)`). It refreshes every 10 s.
- [ ] The dashboard template uses `hx-ext="sse" sse-connect="/gui/events/stream" sse-swap="log" hx-swap="beforeend"` on the log container.
- [ ] **`app.js` behaviour:**
  - The filter form rebuilds the `sse-connect` URL and calls `htmx.process` to reconnect.
  - Pause buffers incoming rows and keeps them out of the DOM; resume flushes them.
  - The log auto-scrolls only when it's already at the bottom.
  - The DOM is capped at 2000 rows.
  - The file uses `addEventListener` only; there are no inline handlers.
- [ ] Each event row shows the time (HH:MM:SS.mmm), level, kind, message and a job link, plus `data` in a `<details>`. All of it is escaped by Jinja; event data is never rendered as HTML.

**Verify:** `uv run pytest tests/service/unit/test_gui_dashboard.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_gui_dashboard.py
import asyncio

import httpx

from research_engine.events.base import Emitter
from research_engine_client.models import EventKind


async def logged_in(app) -> httpx.AsyncClient:  # noqa: ANN001
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://research.localhost")
    await c.post("/login", data={"api_key": "test-key", "next": "/"}, headers={"Origin": "http://research.localhost"})
    return c


async def _read_until(resp: httpx.Response, needle: str) -> str:
    buf = ""
    async for chunk in resp.aiter_text():
        buf += chunk
        if needle in buf:
            return buf
    raise AssertionError("stream ended")


async def test_live_event_within_one_second(app) -> None:  # noqa: ANN001
    async with await logged_in(app) as c, c.stream("GET", "/gui/events/stream?kind=job.") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        await asyncio.sleep(0.05)
        await Emitter(app.state.services.events, "jobX").info(EventKind.FETCH_STARTED, "filtered-out")
        await Emitter(app.state.services.events, "jobX").info(EventKind.JOB_STARTED, "live-<b>marker</b>")
        buf = await asyncio.wait_for(_read_until(resp, "live-"), 1.0)
    assert "event: log" in buf and "filtered-out" not in buf
    assert "&lt;b&gt;marker&lt;/b&gt;" in buf          # escaped, never raw HTML


async def test_history_first(app) -> None:  # noqa: ANN001
    await Emitter(app.state.services.events).warning(EventKind.SEARCH_ENGINE_FAILED, "history-row")
    async with await logged_in(app) as c, c.stream("GET", "/gui/events/stream?level=warning") as resp:
        buf = await asyncio.wait_for(_read_until(resp, "history-row"), 1.0)
    assert "history-row" in buf


async def test_dashboard_and_partials(app) -> None:  # noqa: ANN001
    async with await logged_in(app) as c:
        page = await c.get("/")
        assert page.status_code == 200 and 'sse-connect="/gui/events/stream"' in page.text
        assert (await c.get("/gui/partials/now")).status_code == 200
        health = await c.get("/gui/partials/health")
        assert "searxng" in health.text and "jobs today" in health.text.lower()
```

ASGITransport streaming: httpx's ASGI transport buffers the whole response body in some versions. If `test_live_event_within_one_second` hangs for that reason, write the test against `uvicorn` on an ephemeral port instead, using a `live_server` fixture in `tests/conftest.py` that runs uvicorn in a background task bound to `127.0.0.1:0`. Use superpowers-extended-cc:systematic-debugging to confirm the cause before switching.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement the stream route.** It goes in `gui/routes.py`.

```python
@router.get("/gui/events/stream", response_class=EventSourceResponse)
async def event_stream(request: Request, services: Annotated[Services, Depends(get_services)],
                       level: EventLevel | None = None, job: str | None = None, kind: str | None = None,
                       q: str | None = None) -> AsyncIterator[ServerSentEvent]:
    def matches(e: Event) -> bool:
        return ((level is None or _LEVEL_ORDER[e.level] >= _LEVEL_ORDER[level])
                and (job is None or e.job_id == job)
                and (not kind or e.kind.value.startswith(kind))
                and (not q or q.lower() in e.message.lower()))

    live = services.events.subscribe()
    first_live = asyncio.ensure_future(anext(live))      # register the subscription before reading history
    for e in [e for e in await services.events.tail(1000) if matches(e)][-200:]:
        yield ServerSentEvent(event="log", data=_row(e))
    pending = first_live
    while True:
        done, _ = await asyncio.wait({pending}, timeout=15)
        if not done:
            yield ServerSentEvent(comment="keepalive")
            continue
        e = pending.result()
        if matches(e):
            yield ServerSentEvent(event="log", data=_row(e))
        pending = asyncio.ensure_future(anext(live))
```

`_row(e)` renders `_event_row.html` with `templates.get_template(...).render(e=e)` and strips newlines, because SSE data can't contain raw newlines without being split into multiple `data:` lines. Confirm FastAPI's `ServerSentEvent` handles multi-line data. If it does, keep the newlines. Set the `X-Accel-Buffering: no` header on the response. On client disconnect, cancel `pending` and `aclose()` the generator in a `finally` block. Check the exact names of FastAPI's SSE API against the installed 0.142.x docs (`fastapi.sse`) before writing this, and adjust if the API differs.

Avoiding a race: if a subscriber registered only after the history was read, events emitted in between would be lost. Here the subscription is registered first, so a few events may appear twice instead. That's acceptable; the client de-duplicates by `data-id` in `app.js`.

- [ ] **Step 4: Write the templates and `app.js`.**
  - `_event_row.html` is one `<div class="ev ev-{{e.level}}" data-id="{{e.id}}">…</div>`.
  - In `dashboard.html`:
    - the "Now" panel is `<section hx-get="/gui/partials/now" hx-trigger="load, every 2s, sse:log throttle:500ms">`;
    - the health strip is `hx-trigger="load, every 10s"`;
    - the log is `<div id="log" hx-ext="sse" sse-connect="/gui/events/stream" sse-swap="log" hx-swap="beforeend">`;
    - the filter form has level, job, kind and q, plus a Pause/Resume button.
  - `app.js` implements pause, buffering, de-duplication by `data-id`, auto-scroll, the 2000-row cap, and filter-driven reconnection. Reconnecting means updating `sse-connect`, then `htmx.process(log)`.

- [ ] **Step 5: Run the tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git commit -m "feat(gui): live SSE event log, Now panel and health strip (V1-15, V1-16)"`

---

### Task 6.3: Job detail view

**Goal:** `/jobs/{id}` shows the request, a live event timeline, and the results as a collapsible JSON viewer plus rendered markdown for each document.

**Files:**
- Modify: `packages/research_engine/src/research_engine/gui/routes.py`
- Create: `packages/research_engine/src/research_engine/gui/templates/{job,_json,_document}.html`
- Create: `packages/research_engine/src/research_engine/gui/jsonview.py`
- Test: `tests/service/unit/test_gui_job.py`

**Acceptance Criteria:**
- [ ] `GET /jobs/{id}` returns 200 for a known job and 404 (a plain HTML page) for an unknown one. The page shows:
  - type, status, progress, the created, started and finished times, and errors;
  - the request as a JSON tree;
  - the event timeline: all job events from `bus.query(job_id=…)` plus a live SSE (`/gui/events/stream?job={id}`) while the job isn't terminal.
- [ ] **Results:**
  - For `search_read`: the search results table (rank, title linked to `url`, domain, engines, score), then one collapsible `<details>` per document. Each has a header (title, final URL, method, words, escalation reason, warnings), the **sanitised** markdown, extracted tables as HTML tables (escaped), JSON-LD in a JSON tree, and up to 50 links.
  - For `fetch_batch`: the same document sections, plus failed URLs with their errors.
- [ ] The JSON tree (`jsonview.to_tree(obj)`) renders nested `<details>`/`<ul>` with escaped values. It starts collapsed below depth 2 and truncates strings longer than 2000 characters with a "…" marker. The whole envelope is also downloadable as raw JSON via `GET /v1/jobs/{id}` (a public API link).
- [ ] While the job is running, the status block is refreshed by `hx-trigger="every 2s"` until the job reaches a terminal status. The rendered partial for a terminal job drops the trigger.
- [ ] A test renders a job whose document markdown contains `<script>alert(1)</script>` and asserts the page contains no `<script>alert`.

**Verify:** `uv run pytest tests/service/unit/test_gui_job.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.** Create a job through `services.jobs.submit` with a registered handler that returns a `SearchReadResult` whose document markdown contains a script tag and a table. Wait for it, then GET `/jobs/{id}` while logged in and assert:
  - status 200;
  - "search_read" is present;
  - the rendered `<table>` from the markdown is present;
  - `<script>alert` is absent;
  - a `<details>` element exists;
  - `/jobs/nope` returns 404.

  Unit-test `jsonview.to_tree` separately for escaping (`{"k": "<b>"}` gives `&lt;b&gt;`), the depth-collapse rule and truncation.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement `jsonview.py`.** It's a small recursive function returning `markupsafe.Markup`, built with `markupsafe.escape` on every key and value. Then write the route and templates. Document markdown goes through `render_untrusted_markdown` and is the only `|safe` output, alongside `to_tree`'s `Markup`, which escapes everything it contains.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(gui): job detail view with live timeline, JSON tree and sanitised documents (V1-17)"`

---

### Task 6.4: Test console ("Try it")

**Goal:** `/try` offers Search, Fetch and Search-and-read forms that call the **public REST API** from the browser (cookie-authenticated), a demo dropdown, results shown three ways, a live event trail, "Copy as" snippets, and a list of past runs with one-click re-run.

**Files:**
- Create: `config/demos.yaml`
- Modify: `packages/research_engine/src/research_engine/config_files.py` (add the `Demo` model and `load_demos`)
- Modify: `packages/research_engine/src/research_engine/store/tables.py` (add `TestRunRow`)
- Create: `packages/research_engine/src/research_engine/gui/runs.py` (record and list runs)
- Modify: `packages/research_engine/src/research_engine/gui/routes.py` (`/try`, `POST /gui/render/{kind}`, `POST /gui/runs`, `GET /gui/runs`)
- Create: `packages/research_engine/src/research_engine/gui/templates/{try,_results_search,_results_document,_results_job,_runs}.html`
- Create: `packages/research_engine/src/research_engine/gui/static/try.js`
- Test: `tests/service/unit/test_gui_try.py`, `tests/service/unit/test_demos.py`

**Acceptance Criteria:**
- [ ] `config/demos.yaml` holds at least these 6 demos, each with an `id`, `label`, `kind` (`search`, `fetch` or `search_read`) and a `request` that validates against that kind's request model. A test enforces this.
  - "Compare open-source vector databases" (search, technical);
  - the same as search_read with `top_n=5`;
  - "Python asyncio TaskGroup docs" (fetch, static);
  - "JS-heavy app page" (fetch, auto, using the SPA URL chosen in step 3);
  - "Vendor PDF with tables" (fetch, using the PDF URL from step 3);
  - "Latest news on WebAssembly" (search, news, `time_range=month`).
- [ ] `/try` renders the three forms, the demo `<select>`, a results area with three tabs (Cards, Rendered, Raw JSON), a "Copy as" panel, an events panel and a "Past runs" list.
- [ ] **`try.js` behaviour:**
  - On **Run**, it calls `fetch("/v1/search" | "/v1/fetch" | "/v1/search_read", {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body})`. These are the **same public endpoints** agents use.
  - For `search_read`, it reads the job id and long-polls `GET /v1/jobs/{id}?wait=30` until the job is terminal.
  - It points the events panel's SSE at `/gui/events/stream?job={id}`. For synchronous runs, it uses the run's `request_id` window and falls back to the unfiltered live stream.
  - It POSTs the returned envelope JSON to `/gui/render/{kind}` to get server-rendered, sanitised HTML for the Cards and Rendered tabs. It shows the raw envelope, pretty-printed with `textContent` (never `innerHTML`), in the Raw tab.
  - It records the run with `POST /gui/runs` (`kind`, `request`, `job_id`, `status`, `took_ms`).
- [ ] `POST /gui/render/{search|document|job}`:
  - accepts an envelope JSON body;
  - **validates it with the public models** (`Envelope[SearchResponse]`, `Envelope[Document]` or `Envelope[JobDetail]`), returning 422 for invalid input;
  - returns HTML fragments: **cards** (title, source domain, the engines that agreed, score or quality) and **rendered** output (sanitised markdown, tables, JSON-LD tree);
  - is cookie-authenticated and requires the Origin check.
- [ ] **"Copy as"** produces three snippets, built client-side from the current form values:
  - **curl:** `curl -sS -X POST "$RESEARCH_ENGINE_URL/v1/search" -H "X-API-Key: $RESEARCH_ENGINE_API_KEY" -H 'Content-Type: application/json' -d '<json>'`;
  - **Python:** the typed client (step 7) as `async with ResearchEngineClient(url, api_key=os.environ["RESEARCH_ENGINE_API_KEY"]) as c: await c.search(SearchRequest(...))`;
  - **MCP:** `{"tool": "web_search", "arguments": {...}}`.

  **No snippet ever contains the actual key.** A test checks the rendered `/try` page and `try.js` for the absence of `test-key`.
- [ ] **Past runs:** `GET /gui/runs` lists the last 20 runs (kind, summary, status, time). Re-run fills the form from the stored request and clicks Run. The `test_run` table is created by `init_db`.
- [ ] **Manual acceptance:** the owner opens `/try` in a browser, logs in, and runs each demo, seeing results, structure and the live event trail. This is recorded in the step report as owner confirmation. It is the V1 criterion "a non-developer can run each demo".

**Verify:** `uv run pytest tests/service/unit/test_gui_try.py tests/service/unit/test_demos.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_demos.py
from pathlib import Path

from research_engine.config_files import load_demos
from research_engine_client.models import FetchRequest, SearchReadRequest, SearchRequest

ROOT = Path(__file__).resolve().parents[3]
MODELS = {"search": SearchRequest, "fetch": FetchRequest, "search_read": SearchReadRequest}


def test_demos_valid() -> None:
    demos = load_demos(ROOT / "config" / "demos.yaml")
    assert len(demos) >= 6 and {d.kind for d in demos} == set(MODELS)
    for d in demos:
        MODELS[d.kind].model_validate(d.request)
    assert len({d.id for d in demos}) == len(demos)
```

```python
# tests/service/unit/test_gui_try.py
from pathlib import Path

import httpx

from .test_gui_dashboard import logged_in

STATIC = Path(__file__).resolve().parents[3] / "packages/research_engine/src/research_engine/gui/static"
ORIGIN = {"Origin": "http://research.localhost"}


async def test_try_page_never_contains_key(app) -> None:  # noqa: ANN001
    async with await logged_in(app) as c:
        page = await c.get("/try")
    assert page.status_code == 200 and "test-key" not in page.text
    assert "test-key" not in (STATIC / "try.js").read_text()
    assert "RESEARCH_ENGINE_API_KEY" in (STATIC / "try.js").read_text()


async def test_render_search_validates_and_escapes(app) -> None:  # noqa: ANN001
    async with await logged_in(app) as c:
        env = (await c.post("/v1/search", json={"query": "q", "depth": "quick"}, headers=ORIGIN)).json()
        env["data"]["results"][0]["title"] = "<script>alert(1)</script>"
        r = await c.post("/gui/render/search", json=env, headers=ORIGIN)
        assert r.status_code == 200 and "<script>alert" not in r.text and "&lt;script&gt;" in r.text
        bad = await c.post("/gui/render/search", json={"nope": 1}, headers=ORIGIN)
        assert bad.status_code == 422


async def test_runs_roundtrip(app) -> None:  # noqa: ANN001
    async with await logged_in(app) as c:
        r = await c.post("/gui/runs", json={"kind": "search", "request": {"query": "q"}, "job_id": None,
                                            "status": "done", "took_ms": 12}, headers=ORIGIN)
        assert r.status_code == 201
        listing = await c.get("/gui/runs")
        assert "q" in listing.text
        foreign = await c.post("/gui/runs", json={"kind": "search", "request": {}, "job_id": None,
                                                  "status": "done", "took_ms": 1},
                               headers={"Origin": "http://evil.example"})
        assert foreign.status_code == 403
```

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement the pieces.**
  - **Demos loader:** `Demo(BaseModel)` with `id`, `label`, `kind: Literal["search","fetch","search_read"]`, `request: dict[str, Any]` and `description: str`. `load_demos(path) -> list[Demo]`.
  - **`TestRunRow`:** `id` (int pk), `kind`, `request_json`, `job_id`, `status`, `took_ms` and `created_at`. It's created by `init_db` (a new table, so no migration is needed).
  - **`gui/runs.py`:** `record(engine, RunIn) -> int` and `recent(engine, n=20)`.
  - **Render routes:** validate with the public models, then render the result templates. The search cards show title (escaped), domain, `engines|join(", ")`, score and published date. The document view shows `render_untrusted_markdown(doc.markdown)`, the tables loop and `to_tree(structured_data)`.
  - **`try.js`:** form handling, the demo select (demo JSON is embedded with Jinja `|tojson` inside `<script type="application/json" id="demos">`, which CSP allows because it isn't executed), the Run logic, the tabs, copy-to-clipboard via `navigator.clipboard.writeText`, and the runs list refresh.
  - The SPA and PDF demo URLs reuse the constants verified in step 3 (`tests/integration/live_urls.py`). Copy the values into `demos.yaml`, and add a test asserting they match.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Manual smoke run** (needs the dev stack plus a local uvicorn). Start it with `uv run uvicorn research_engine.app:create_app --factory --host 127.0.0.1 --port 0`, using env from `.env` plus the dev URLs. Open the printed URL through an SSH tunnel from the owner's MacBook, or ask the owner to. Run each demo and confirm the event trail streams live. Take screenshots if a browser tool is available. **Ask the owner to confirm the V1-21 acceptance criterion.**

- [ ] **Step 6: Run the full step check** (Global Constraint 11).

- [ ] **Step 7: Commit.** `git commit -m "feat(gui): test console with demos, three result views, live events, copy-as and re-run (V1-21)"`

---

**End of step 6:** request code review, report to the owner with evidence (test output and screenshots), get the owner's confirmation of the V1-21 manual criterion, and **stop** for approval. After approval, run `git push`.
