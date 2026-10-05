"""GUI test console ("Try it"): page, render endpoints, past runs, copy-as snippets (V1-21)."""

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from research_engine.events.base import Emitter
from research_engine.gui import runs
from research_engine_client.models import EventKind, JobType, SearchReadRequest, SearchRequest
from sqlalchemy import func, inspect
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .test_gui_dashboard import live_client, log_texts, logged_in
from .test_gui_job import EVIL_MD, result, run_job

STATIC = (
    Path(__file__).resolve().parents[3] / "packages/research_engine/src/research_engine/gui/static"
)
TRY_JS = STATIC / "try.js"
ORIGIN = {"Origin": "http://research.localhost"}
EVIL = {"Origin": "http://evil.example"}


def run_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "search",
        "request": {"query": "q"},
        "job_id": None,
        "status": "done",
        "took_ms": 12,
    }
    body.update(kw)
    return body


# --- the page ----------------------------------------------------------------------------------


async def test_try_page_never_contains_key(app) -> None:
    async with logged_in(app) as c:
        page = await c.get("/try")
        await c.post("/gui/runs", json=run_body(), headers=ORIGIN)
        listing = await c.get("/gui/runs")
    assert page.status_code == 200 and "test-key" not in page.text
    assert "test-key" not in listing.text
    assert "test-key" not in TRY_JS.read_text()
    assert "RESEARCH_ENGINE_API_KEY" in TRY_JS.read_text()


async def test_try_page_structure(app) -> None:
    async with logged_in(app) as c:
        page = await c.get("/try")
    html = page.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    for form in ("form-search", "form-fetch", "form-search_read"):
        assert f'id="{form}"' in html
    assert '<select id="demo"' in html
    for tab in ("cards", "rendered", "raw"):
        assert f'data-view="{tab}"' in html
    for snippet in ("copy-curl", "copy-python", "copy-mcp"):
        assert f'id="{snippet}"' in html
    assert 'id="trail"' in html
    assert 'id="runs"' in html and 'hx-get="/gui/runs"' in html
    assert '<script src="/static/try.js" defer></script>' in html
    # every <script> is external, or an inert JSON data block (CSP: no inline script)
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag or 'type="application/json"' in tag, tag
    assert "style=" not in html and not re.search(r"<[^>]*\son[a-z]+=", html)  # no inline handlers
    block = re.search(r'<script type="application/json" id="demos">(.*?)</script>', html, re.S)
    assert block is not None
    demos = json.loads(block.group(1))
    assert len(demos) >= 6 and {d["kind"] for d in demos} == {"search", "fetch", "search_read"}
    assert all(f'<option value="{d["id"]}">' in html for d in demos)
    # every field a demo sets has a form field to fill (dotted names for nested objects)
    for d in demos:
        for path in _paths(d["request"]):
            form = html.split(f'id="form-{d["kind"]}"')[1].split("</form>")[0]
            assert f'name="{path}"' in form, (d["id"], path)


def _paths(obj: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in obj.items():
        if isinstance(v, dict):
            out += _paths(v, f"{prefix}{k}.")  # pyright: ignore[reportUnknownArgumentType]
        else:
            out.append(prefix + k)
    return out


async def test_try_page_demo_json_cannot_break_out(app, tmp_path: Path, monkeypatch) -> None:
    evil = tmp_path / "demos.yaml"
    evil.write_text(
        "demos:\n"
        "  - id: evil\n"
        '    label: "</script><script>alert(1)</script>"\n'
        '    description: "<!-- & \' \\""\n'
        "    kind: search\n"
        '    request: {query: "</script><b>x</b>"}\n'
    )
    monkeypatch.setattr(app.state.services.settings, "demos_file", evil)
    async with logged_in(app) as c:
        page = await c.get("/try")
    assert page.status_code == 200
    assert "<script>alert" not in page.text and "<b>x</b>" not in page.text
    block = re.search(r'<script type="application/json" id="demos">(.*?)</script>', page.text, re.S)
    assert block is not None
    assert json.loads(block.group(1))[0]["request"]["query"] == "</script><b>x</b>"


async def test_try_page_survives_a_broken_demos_file(app, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app.state.services.settings, "demos_file", tmp_path / "missing.yaml")
    async with logged_in(app) as c:
        page = await c.get("/try")
    assert page.status_code == 200
    assert "Demos unavailable" in page.text and 'id="form-search"' in page.text


async def test_try_requires_login(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost"
    ) as c:
        r = await c.get("/try")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/try"
        assert (await c.get("/gui/runs")).status_code == 303


# --- render ------------------------------------------------------------------------------------


async def test_render_search_validates_and_escapes(app) -> None:
    async with logged_in(app) as c:
        env = (
            await c.post("/v1/search", json={"query": "q", "depth": "quick"}, headers=ORIGIN)
        ).json()
        env["data"]["results"][0]["title"] = "<script>alert(1)</script>"
        r = await c.post("/gui/render/search", json=env, headers=ORIGIN)
        assert r.status_code == 200 and "<script>alert" not in r.text and "&lt;script&gt;" in r.text
        bad = await c.post("/gui/render/search", json={"nope": 1}, headers=ORIGIN)
        assert bad.status_code == 422


async def test_render_search_cards_and_table(app) -> None:
    async with logged_in(app) as c:
        env = (
            await c.post("/v1/search", json={"query": "q", "depth": "quick"}, headers=ORIGIN)
        ).json()
        env["data"]["results"][0]["url"] = "javascript:alert(1)"
        r = await c.post("/gui/render/search", json=env, headers=ORIGIN)
    html = r.text
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert '<div data-view="cards">' in html and '<div data-view="rendered">' in html
    cards, rendered = html.split('<div data-view="rendered">')
    first = env["data"]["results"][1]
    assert cards.count('class="card') == len(env["data"]["results"])
    assert first["domain"] in cards and "a, b" in cards and str(first["score"]) in cards
    assert '<table class="search-results">' in rendered
    assert 'href="javascript:' not in html  # non-http URLs are never links


async def test_render_document_sanitises(app) -> None:
    async with logged_in(app) as c:
        env = (
            await c.post("/v1/fetch", json={"url": "https://blog.example/post"}, headers=ORIGIN)
        ).json()
        assert env["data"]["title"] == "How metasearch engines merge results"
        env["data"]["markdown"] = EVIL_MD
        env["data"]["title"] = "<img src=x onerror=alert(9)>"
        env["data"]["structured_data"]["json_ld"] = [{"<k>": "<script>alert(4)</script>"}]
        env["data"]["tables"] = [{"caption": "<i>c</i>", "headers": ["h"], "rows": [["<b>1</b>"]]}]
        r = await c.post("/gui/render/document", json=env, headers=ORIGIN)
    html = r.text
    assert r.status_code == 200
    for raw in ("<script", "<img", 'href="javascript:', "<i>c</i>", "<b>1</b>", "<k>"):
        assert raw not in html, raw
    assert not re.search(r"<[^>]*\son[a-z]+=", html)  # no event-handler attribute on any tag
    assert "&lt;img src=x onerror=alert(9)&gt;" in html
    assert "<h1>Title</h1>" in html  # markdown is rendered (sanitised), not escaped wholesale
    assert "JSON-LD" in html and "&lt;k&gt;" in html
    assert 'class="card' in html.split('<div data-view="rendered">')[0]
    assert '<details class="document panel"' in html  # the job page's _document.html, reused


async def test_render_job_search_read(app) -> None:
    req = SearchReadRequest(search=SearchRequest(query="q <b>x</b>"))
    job_id = await run_job(app, JobType.SEARCH_READ, req, result(markdown=EVIL_MD))
    async with logged_in(app) as c:
        env = (await c.get(f"/v1/jobs/{job_id}")).json()
        r = await c.post("/gui/render/job", json=env, headers=ORIGIN)
    html = r.text
    assert r.status_code == 200
    assert "<script" not in html and "<i>one</i>" not in html and "Hit &lt;i&gt;one" in html
    assert '<table class="search-results">' in html and '<details class="document panel"' in html
    assert f'href="/jobs/{job_id}"' in html
    assert "duckduckgo, brave" in html.split('<div data-view="rendered">')[0]


async def test_render_error_envelope(app) -> None:
    async with logged_in(app) as c:
        err = await c.post("/v1/fetch", json={"url": "https://nowhere.example/"}, headers=ORIGIN)
        assert err.status_code >= 400
        env = err.json()
        env["errors"][0]["message"] = "<script>alert(5)</script>"
        r = await c.post("/gui/render/document", json=env, headers=ORIGIN)
    assert r.status_code == 200
    assert "&lt;script&gt;alert(5)" in r.text and "<script>" not in r.text
    assert env["errors"][0]["code"] in r.text


async def test_render_kind_size_and_auth(app) -> None:
    async with logged_in(app) as c:
        env = (
            await c.post("/v1/search", json={"query": "q", "depth": "quick"}, headers=ORIGIN)
        ).json()
        assert (await c.post("/gui/render/nope", json=env, headers=ORIGIN)).status_code == 404
        assert (await c.post("/gui/render/search", json=env, headers=EVIL)).status_code == 403
        assert (await c.post("/gui/render/search", json=env)).status_code == 403  # no Origin
        # The wrong model for the kind is a validation error, not a crash.
        assert (await c.post("/gui/render/document", json=env, headers=ORIGIN)).status_code == 422
        not_json = await c.post("/gui/render/search", content=b"{", headers=ORIGIN)
        assert not_json.status_code == 422
        big = b'{"pad": "' + b"x" * (6 * 1024 * 1024) + b'"}'
        r = await c.post("/gui/render/search", content=big, headers=ORIGIN)
        assert r.status_code == 413
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost"
    ) as anon:
        r = await anon.post("/gui/render/search", json=env, headers=ORIGIN)
        assert r.status_code == 303  # no session: to the login page


async def test_render_size_limit_without_content_length(app) -> None:
    async def chunks():
        for _ in range(7):
            yield b"x" * (1024 * 1024)

    async with logged_in(app) as c:
        r = await c.post("/gui/render/search", content=chunks(), headers=ORIGIN)
    assert r.status_code == 413


# --- runs --------------------------------------------------------------------------------------


async def test_runs_roundtrip(app) -> None:
    async with logged_in(app) as c:
        r = await c.post("/gui/runs", json=run_body(), headers=ORIGIN)
        assert r.status_code == 201
        listing = await c.get("/gui/runs")
        assert "q" in listing.text
        foreign = await c.post("/gui/runs", json=run_body(request={}, took_ms=1), headers=EVIL)
        assert foreign.status_code == 403


async def test_runs_listing_content(app) -> None:
    job = "ab" * 16
    async with logged_in(app) as c:
        evil = run_body(request={"query": "<script>x</script>"})
        await c.post("/gui/runs", json=evil, headers=ORIGIN)
        await c.post(
            "/gui/runs",
            json=run_body(
                kind="search_read",
                request={"search": {"query": "deep 'q'"}, "top_n": 5},
                job_id=job,
                status="partial",
            ),
            headers=ORIGIN,
        )
        await c.post(
            "/gui/runs",
            json=run_body(kind="fetch", request={"url": "https://blog.example/post"}),
            headers=ORIGIN,
        )
        listing = await c.get("/gui/runs")
    html = listing.text
    assert listing.headers["cache-control"] == "no-store"
    assert "<script>x" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert "deep &#39;q&#39;" in html and "https://blog.example/post" in html
    assert f'href="/jobs/{job}"' in html and "partial" in html
    # newest first; the re-run button carries kind and the stored request
    assert html.index("blog.example") < html.index("deep") < html.index("&lt;script")
    rerun = r'<button type="button" class="rerun" data-kind="(\w+)" data-request="([^"]*)"'
    buttons = re.findall(rerun, html)
    assert [k for k, _ in buttons] == ["fetch", "search_read", "search"]
    unescaped = buttons[1][1].replace("&#34;", '"').replace("&#39;", "'").replace("&amp;", "&")
    assert json.loads(unescaped) == {"search": {"query": "deep 'q'"}, "top_n": 5}


async def test_runs_lists_last_20(app) -> None:
    async with logged_in(app) as c:
        for i in range(25):
            body = run_body(request={"query": f"query-{i:02d}"})
            assert (await c.post("/gui/runs", json=body, headers=ORIGIN)).status_code == 201
        html = (await c.get("/gui/runs")).text
    assert "query-24" in html and "query-05" in html and "query-04" not in html
    assert html.count('class="rerun"') == 20


async def test_runs_empty_listing(app) -> None:
    async with logged_in(app) as c:
        html = (await c.get("/gui/runs")).text
    assert "No runs yet" in html


@pytest.mark.parametrize(
    "override",
    [
        {"kind": "batch"},
        {"status": "weird"},
        {"request": ["not", "an", "object"]},
        {"request": {"query": "x" * (65 * 1024)}},
        {"job_id": "NOT-HEX!"},
        {"took_ms": -1},
        {"api_key": "test-key"},
        {"headers": {"X-API-Key": "k"}},
    ],
)
async def test_runs_validation(app, override: dict[str, Any]) -> None:
    async with logged_in(app) as c:
        r = await c.post("/gui/runs", json=run_body(**override), headers=ORIGIN)
        assert r.status_code == 422, r.text
        assert "No runs yet" in (await c.get("/gui/runs")).text


async def test_runs_body_cap(app) -> None:
    async with logged_in(app) as c:
        r = await c.post("/gui/runs", content=b"x" * (300 * 1024), headers=ORIGIN)
    assert r.status_code == 413


async def test_runs_pruned_to_last_500(app) -> None:
    engine = app.state.services.engine
    for i in range(runs.MAX_ROWS + 7):
        await runs.record(
            engine,
            runs.RunIn(
                kind=runs.RunKind.SEARCH,
                request={"query": f"q{i}"},
                job_id=None,
                status=runs.RunStatus.DONE,
                took_ms=i,
            ),
        )
    async with AsyncSession(engine) as s:
        count = (await s.exec(select(func.count()).select_from(runs.TestRunRow))).one()
        oldest = (await s.exec(select(func.min(runs.TestRunRow.took_ms)))).one()
    assert count == runs.MAX_ROWS == 500
    assert oldest == 7
    recent = await runs.recent(engine)
    assert len(recent) == 20 and recent[0].took_ms == runs.MAX_ROWS + 6
    assert recent[0].created_at.tzinfo is not None  # read back as aware UTC


async def test_test_run_table_created_by_init_db(app) -> None:
    async with app.state.services.engine.connect() as conn:
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        cols = await conn.run_sync(
            lambda sync: {c["name"] for c in inspect(sync).get_columns("test_run")}
        )
    assert "test_run" in names
    assert cols == {"id", "kind", "request_json", "job_id", "status", "took_ms", "created_at"}


# --- live-only event stream for synchronous runs -----------------------------------------------


async def test_stream_without_history_signals_ready(app, live_server: str) -> None:
    bus = app.state.services.events
    await Emitter(bus).info(EventKind.SEARCH_DONE, "old-history-row")
    before = bus.subscriber_count
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", "/gui/events/stream?history=0") as resp,
    ):
        chunks = resp.aiter_text()
        buf = ""

        async def until(needle: str) -> None:
            nonlocal buf
            while needle not in buf[: buf.rfind("\n\n")]:
                buf += await anext(chunks)

        await asyncio.wait_for(until("event: ready"), 2.0)
        assert bus.subscriber_count > before  # subscribed before "ready" was sent
        await Emitter(bus).info(EventKind.SEARCH_STARTED, "live-row")
        await asyncio.wait_for(until("live-row"), 2.0)
    assert "old-history-row" not in buf
    assert any("live-row" in t for t in log_texts(buf))


# --- try.js ------------------------------------------------------------------------------------


def test_try_js_rules() -> None:
    js = TRY_JS.read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    assert "eval(" not in js and "new Function" not in js and "document.write" not in js
    assert ".onclick" not in js and "addEventListener" in js
    for endpoint in ("/v1/search", "/v1/fetch", "/v1/search_read", "/gui/render/", "/gui/runs"):
        assert endpoint in js
    assert 'credentials: "same-origin"' in js and "wait=30" in js
    assert "navigator.clipboard.writeText" in js and "textContent" in js
    assert "JSON.stringify(env, null, 2)" in js


NODE = shutil.which("node")


def snippets(kind: str, request: dict[str, Any]) -> dict[str, str]:
    assert NODE is not None
    script = (
        "const t = require(process.argv[1]);"
        "let s = ''; process.stdin.on('data', d => s += d);"
        "process.stdin.on('end', () => { const a = JSON.parse(s);"
        " process.stdout.write(JSON.stringify(t.snippets(a.kind, a.request))); });"
    )
    out = subprocess.run(  # noqa: S603
        [NODE, "-e", script, str(TRY_JS)],
        input=json.dumps({"kind": kind, "request": request}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


TR = "month"
TRICKY = 'it\'s "quoted" $HOME `id` \\ ü \n new line'
CASES = [
    ("search", "/v1/search", "web_search", {"query": TRICKY, "intent": "news", "time_range": TR}),
    (
        "fetch",
        "/v1/fetch",
        "web_fetch",
        {"url": "https://example.com/a'b", "mode": "browser", "formats": ["markdown", "html"]},
    ),
    (
        "search_read",
        "/v1/search_read",
        "search_and_read",
        {"search": {"query": "o'brien", "intent": "technical"}, "top_n": 5},
    ),
]


@pytest.mark.parametrize(("kind", "path", "tool", "request_"), CASES)
def test_copy_as_snippets(kind: str, path: str, tool: str, request_: dict[str, Any]) -> None:
    if NODE is None:
        # CI installs node (actions/setup-node), so a missing node there is a broken runner,
        # not a reason to silently skip the only test that executes the snippet builders.
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI: the copy-as snippet tests need it")
        pytest.skip("node is not installed")
    s = snippets(kind, request_)
    assert set(s) == {"curl", "python", "mcp"}
    for text in s.values():
        assert "test-key" not in text
    # curl: run it through a real shell with curl replaced by a function that dumps its argv.
    bash = shutil.which("bash")
    assert bash is not None
    env = {
        "PATH": os.environ.get("PATH", ""),
        "RESEARCH_ENGINE_URL": "http://re.test",
        "RESEARCH_ENGINE_API_KEY": "env-secret",
        "HOME": "/nonexistent",
    }
    out = subprocess.run(  # noqa: S603
        [bash, "-c", "curl() { printf '%s\\0' \"$@\"; }\n" + s["curl"]],
        capture_output=True,
        env=env,
        check=True,
        timeout=30,
    )
    argv = out.stdout.decode().split("\0")[:-1]
    assert argv[argv.index("-X") + 1] == "POST"
    assert f"http://re.test{path}" in argv
    assert "X-API-Key: env-secret" in argv  # taken from the environment at run time
    assert "$RESEARCH_ENGINE_API_KEY" in s["curl"]
    assert json.loads(argv[argv.index("-d") + 1]) == request_
    # python: valid syntax, the key comes from the environment, the request round-trips.
    compile(s["python"], "<snippet>", "exec")
    assert 'api_key=os.environ["RESEARCH_ENGINE_API_KEY"]' in s["python"]
    assert "ResearchEngineClient(" in s["python"]
    literal = re.search(r"^REQUEST_JSON = (.*)$", s["python"], re.M)
    assert literal is not None and "model_validate_json(REQUEST_JSON)" in s["python"]
    assert json.loads(ast.literal_eval(literal.group(1))) == request_
    # mcp: plain JSON with the tool name and arguments taken from the request.
    mcp = json.loads(s["mcp"])
    assert mcp["tool"] == tool and isinstance(mcp["arguments"], dict)
    if kind == "search":
        assert mcp["arguments"]["query"] == request_["query"]
        assert mcp["arguments"]["time_range"] == "month"
    elif kind == "fetch":
        assert mcp["arguments"] == {"url": request_["url"], "mode": "browser", "include_html": True}
    else:
        assert mcp["arguments"]["query"] == "o'brien" and mcp["arguments"]["top_n"] == 5
