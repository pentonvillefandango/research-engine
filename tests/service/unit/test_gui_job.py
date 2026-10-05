"""GUI job detail page, status partial and JSON tree (V1-17)."""

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from markupsafe import Markup
from research_engine.events.base import Emitter
from research_engine.gui import jsonview
from research_engine.jobs.context import JobContext
from research_engine_client.models import (
    BatchFetchRequest,
    BatchFetchResult,
    ErrorCode,
    ErrorDetail,
    EventKind,
    FailedUrl,
    JobType,
    Link,
    RankedDocument,
    SearchReadRequest,
    SearchReadResult,
    SearchRequest,
    SearchResponse,
    SearchResult,
    Table,
)

from .helpers import doc
from .test_gui_dashboard import logged_in

BATCH_REQ = BatchFetchRequest(urls=("https://a.example/",))
SEARCH_REQ = SearchReadRequest(search=SearchRequest(query="q <b>x</b>"))
EVIL_MD = (
    "# Title\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n"
    "[click](javascript:alert(3))\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
)


def result(url: str = "https://r.example/page", **doc_kw: Any) -> SearchReadResult:
    d = doc(url).model_copy(update=doc_kw)
    sr = SearchResult(
        rank=1,
        url=url,
        canonical_url=url,
        title="Hit <i>one</i>",
        snippet="s",
        domain="r.example",
        engines=["duckduckgo", "brave"],
        score=1.5,
    )
    return SearchReadResult(
        search=SearchResponse(query="q", results=[sr]),
        documents=[RankedDocument(search_rank=1, search_score=1.5, document=d)],
    )


async def run_job(app, type_: JobType, req, value) -> str:
    jobs = app.state.services.jobs

    async def handler(ctx: JobContext):
        return value

    jobs.register(type_, handler)
    job = await jobs.submit(type_, req)
    await jobs.wait(job.id, 5)
    return job.id


def strip_base_scripts(html: str) -> str:
    return re.sub(r"<script\b[^>]*\bsrc=[^>]*></script>", "", html)


# --- jsonview ---------------------------------------------------------------------------------


def test_tree_escapes_keys_and_values() -> None:
    out = jsonview.to_tree({"<k>": "<b>", "n": [1, None, True, 2.5]})
    assert isinstance(out, Markup)
    assert "&lt;b&gt;" in out and "&lt;k&gt;" in out
    assert "<b>" not in out and "<k>" not in out
    assert "<details" in out and "<ul" in out


def test_tree_scalar_and_empty() -> None:
    assert "7" in jsonview.to_tree(7)
    assert "{}" in jsonview.to_tree({}) and "[]" in jsonview.to_tree([])
    assert "datetime" not in jsonview.to_tree({"t": datetime(2026, 1, 1, tzinfo=UTC)})


def test_tree_collapses_below_depth_two() -> None:
    out = str(jsonview.to_tree({"a": {"b": {"c": {"d": 1}}}}))
    opens = re.findall(r"<details( open)?>", out)
    assert opens == [" open", " open", "", ""]  # depths 0 and 1 open; 2 and 3 collapsed
    assert str(jsonview.to_tree({"x": {"y": 1}}, depth=2)).count(" open") == 0


def test_tree_truncates_long_strings() -> None:
    out = str(jsonview.to_tree({"s": "a" * 2000 + "b" * 7}))
    assert "… (7 more chars)" in out and "b" not in out.replace("more chars", "")
    exact = str(jsonview.to_tree("a" * 2000))
    assert "more chars" not in exact


def test_tree_depth_limit() -> None:
    obj: Any = "leaf"
    for _ in range(100):
        obj = [obj]
    out = str(jsonview.to_tree(obj))
    assert "… truncated" in out and "leaf" not in out
    assert out.count("<details") <= jsonview.MAX_DEPTH


def test_tree_node_limit() -> None:
    out = str(jsonview.to_tree({"big": list(range(100_000)), "later": "zzz"}))
    assert "… truncated" in out and "zzz" not in out
    assert out.count("<li") <= jsonview.MAX_NODES + 5


def test_tree_is_cyclic_safe() -> None:
    a: list[Any] = []
    a.append(a)
    assert "… truncated" in jsonview.to_tree(a)


# --- page -------------------------------------------------------------------------------------


async def test_search_read_page(app) -> None:
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, result(markdown=EVIL_MD))
    async with logged_in(app) as c:
        r = await c.get(f"/jobs/{jid}")
    assert r.status_code == 200
    html = r.text
    assert "search_read" in html and "done" in html and jid in html
    assert "<table" in html and "<td>1</td>" in html  # table rendered from the markdown
    assert "<details" in html
    assert "q &lt;b&gt;x&lt;/b&gt;" in html  # request tree, escaped
    assert f'href="/v1/jobs/{jid}"' in html  # raw JSON link
    assert "Hit &lt;i&gt;one&lt;/i&gt;" in html and "duckduckgo" in html and "1.5" in html
    assert "r.example" in html


async def test_untrusted_markdown_is_inert(app) -> None:
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, result(markdown=EVIL_MD))
    async with logged_in(app) as c:
        html = strip_base_scripts((await c.get(f"/jobs/{jid}")).text)
    assert "<script" not in html and "<img" not in html
    assert not re.search(r"<[^>]*\sonerror\s*=", html)  # only ever escaped text, never an attribute
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert not re.search(r"""href\s*=\s*["']?\s*javascript:""", html, re.I)
    assert "click" in html  # the link text survives (as inert text)


async def test_page_has_no_inline_script_style_or_hx_on(app) -> None:
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, result())
    async with logged_in(app) as c:
        html = strip_base_scripts((await c.get(f"/jobs/{jid}")).text)
    assert "<script" not in html and "<style" not in html
    assert not re.search(r"\sstyle\s*=", html) and "hx-on" not in html


async def test_document_header_fields(app) -> None:
    jid = await run_job(
        app,
        JobType.SEARCH_READ,
        SEARCH_REQ,
        result(title="T<x>", warnings=["warn <1>"], word_count=321),
    )
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert "T&lt;x&gt;" in html and "warn &lt;1&gt;" in html
    assert "321" in html and "static" in html
    assert html.count('<details class="document ') == 1


async def test_batch_page_with_failed_urls(app) -> None:
    value = BatchFetchResult(
        documents=[doc("https://ok.example/")],
        failed=[
            FailedUrl(
                url="https://bad.example/<x>",
                error=ErrorDetail(
                    code=ErrorCode.FETCH_FAILED, message="boom <b>", retryable=True, source="s"
                ),
            )
        ],
    )
    jid = await run_job(app, JobType.FETCH_BATCH, BATCH_REQ, value)
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert "fetch_batch" in html and "bad.example/&lt;x&gt;" in html
    assert "boom &lt;b&gt;" in html and "<b>" not in html
    assert "https://ok.example/" in html
    assert "<table" not in html or "Search results" not in html


async def test_job_errors_and_times_shown(app) -> None:
    jobs = app.state.services.jobs

    async def h(ctx: JobContext):
        ctx.add_error(ErrorDetail(code=ErrorCode.FETCH_FAILED, message="soft <i>", retryable=False))
        return BatchFetchResult(documents=[], failed=[])

    jobs.register(JobType.FETCH_BATCH, h)
    job = await jobs.submit(JobType.FETCH_BATCH, BATCH_REQ)
    await jobs.wait(job.id, 5)
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{job.id}")).text
    assert "partial" in html and "soft &lt;i&gt;" in html
    assert html.count("<time") >= 3  # created, started, finished


async def test_unknown_and_invalid_ids_404_html(app) -> None:
    async with logged_in(app) as c:
        unknown = await c.get("/jobs/" + "0" * 32)
        nope = await c.get("/jobs/nope")
    for r in (unknown, nope):
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/html")
        assert "Research Engine" in r.text and "not found" in r.text.lower()


async def test_invalid_id_does_not_touch_db(app) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("DB touched")

    jobs = app.state.services.jobs
    jobs.get = boom
    app.state.services.events.query = boom
    async with logged_in(app) as c:
        for bad in ("NOPE", "g" * 8, "a" * 65, "x%20y"):
            for path in (f"/jobs/{bad}", f"/gui/partials/job/{bad}/status"):
                assert (await c.get(path)).status_code == 404, path


async def test_requires_login(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost"
    ) as c:
        for path in ("/jobs/abc", "/gui/partials/job/abc/status"):
            r = await c.get(path)
            assert r.status_code == 303 and r.headers["location"].startswith("/login")


# --- status partial / polling -----------------------------------------------------------------


async def test_status_polls_while_running_and_stops_when_terminal(app) -> None:
    jobs = app.state.services.jobs
    started, release = asyncio.Event(), asyncio.Event()

    async def h(ctx: JobContext):
        await ctx.progress(1, 4, "https://cur.example/")
        started.set()
        await release.wait()
        return BatchFetchResult(documents=[], failed=[])

    jobs.register(JobType.FETCH_BATCH, h)
    job = await jobs.submit(JobType.FETCH_BATCH, BATCH_REQ)
    await asyncio.wait_for(started.wait(), 2)
    url = f"/gui/partials/job/{job.id}/status"
    async with logged_in(app) as c:
        running = await c.get(url)
        page = (await c.get(f"/jobs/{job.id}")).text
        assert running.status_code == 200
        assert f'hx-get="{url}' in running.text and 'hx-trigger="every 2s"' in running.text
        assert "1/4" in running.text and "cur.example" in running.text
        assert 'hx-trigger="every 2s"' in page
        assert f'sse-connect="/gui/events/stream?job={job.id}"' in page
        assert 'hx-ext="sse"' in page
        assert "HX-Refresh" not in running.headers

        release.set()
        await jobs.wait(job.id, 5)
        done = await c.get(url)
        polled = await c.get(url + "?live=1")
        page = (await c.get(f"/jobs/{job.id}")).text
    assert "hx-trigger" not in done.text and "hx-get" not in done.text and "done" in done.text
    assert "HX-Refresh" not in done.headers
    assert polled.headers["HX-Refresh"] == "true"  # a live poll that ends: reload for results
    assert "hx-trigger" not in page and "sse-connect" not in page


def test_app_js_dedupes_timeline_rows() -> None:
    from pathlib import Path

    js = (Path(jsonview.__file__).parent / "static/app.js").read_text()
    assert 'getElementById("timeline")' in js and "htmx:sseBeforeMessage" in js
    assert "evt.preventDefault()" in js and "innerHTML" not in js


async def test_sse_url_is_encoded_and_fixed_to_job(app) -> None:
    from research_engine.gui.routes import templates

    html = templates.env.get_template("_job_timeline.html").render(
        job_id="a&b c", rows=[], live=True, more=False
    )
    assert 'sse-connect="/gui/events/stream?job=a%26b%20c"' in html


# --- timeline ---------------------------------------------------------------------------------


async def test_timeline_history_ascending_and_filtered_to_job(app) -> None:
    jid = await run_job(
        app, JobType.FETCH_BATCH, BATCH_REQ, BatchFetchResult(documents=[], failed=[])
    )
    bus = app.state.services.events
    await Emitter(bus, jid).info(EventKind.FETCH_DONE, "first <mark>")
    await Emitter(bus, jid).info(EventKind.FETCH_DONE, "second")
    await Emitter(bus, "other").info(EventKind.FETCH_DONE, "someone-else")
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert html.index("first &lt;mark&gt;") < html.index("second")
    assert "someone-else" not in html and "<mark>" not in html
    assert 'class="ev ' in html and "job.queued" in html


async def _job_with_events(app, n: int) -> str:
    jid = await run_job(
        app, JobType.FETCH_BATCH, BATCH_REQ, BatchFetchResult(documents=[], failed=[])
    )
    em = Emitter(app.state.services.events, jid)
    for i in range(n):
        await em.debug(EventKind.JOB_PROGRESS, f"ev-{i:04d}")
    return jid


async def test_long_timeline_shows_oldest_and_newest_250(app) -> None:
    # job.queued/started/done are 3 more events: 603 in total, so 103 are omitted.
    jid = await _job_with_events(app, 600)
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert html.count('class="ev ') == 500
    assert "job.queued" in html and "job.done" in html  # both ends are kept
    assert "ev-0246" in html and "ev-0247" not in html  # 250 oldest = 3 job events + ev-0000..0246
    assert "… 103 events omitted …" in html
    assert html.index("job.queued") < html.index("omitted") < html.index("ev-0599")
    ids = re.findall(r'data-id="(\d+)"', html)
    assert ids == sorted(ids, key=int) and len(set(ids)) == len(ids)


async def test_short_timeline_has_no_marker_or_duplicates(app) -> None:
    jid = await _job_with_events(app, 397)  # 400 events with the job's own three
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    ids = re.findall(r'data-id="(\d+)"', html)
    assert len(ids) == 400 == len(set(ids))
    assert "omitted" not in html


# --- links and size limits --------------------------------------------------------------------


async def test_external_links_http_only_and_hardened(app) -> None:
    good = "https://good.example/a?x=1&y=<2>"
    res = result(final_url="https://final.example/p")
    bad = SearchResult.model_construct(
        rank=2,
        url="javascript:alert(1)",
        canonical_url="javascript:alert(1)",
        title="Evil",
        snippet="",
        domain="evil",
        engines=["e"],
        score=0.1,
        published_at=None,
    )
    ok = res.search.results[0].model_copy(update={"url": good})
    search = res.search.model_copy(update={"results": [ok, bad]})
    res = res.model_copy(update={"search": search})
    d = res.documents[0].document.model_copy(
        update={
            "final_url": "data:text/html,<b>x",
            "links": [
                Link(url="javascript:alert(9)", text="jslink", external=True),
                Link(url="https://l.example/", text="fine", external=True),
            ],
        }
    )
    res = res.model_copy(
        update={"documents": [res.documents[0].model_copy(update={"document": d})]}
    )
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, res)
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert not re.search(r'href="\s*(javascript|data):', html, re.I)
    assert "Evil" in html and "jslink" in html  # still shown, as plain text
    m = re.search(r'<a [^>]*href="https://good\.example/a\?x=1&amp;y=&lt;2&gt;"[^>]*>', html)
    assert m, html
    for attr in ('rel="noopener noreferrer nofollow"', 'target="_blank"'):
        assert attr in m.group(0)
    link = re.search(r'<a [^>]*href="https://l\.example/"[^>]*>', html)
    assert link and 'rel="noopener noreferrer nofollow"' in link.group(0)


async def test_markdown_truncated_with_link_to_full_json(app) -> None:
    md = "word " * 100_000  # 500 KB
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, result(markdown=md))
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert len(html) < 300_000
    assert "truncated" in html.lower() and f'href="/v1/jobs/{jid}"' in html


async def test_links_tables_and_rows_are_capped(app) -> None:
    links = [
        Link(url=f"https://l.example/{i}", text=f"lnk{i:03d}", external=True) for i in range(80)
    ]
    tables = [Table(headers=["h<1>"], rows=[[f"r{r}t{t}"] for r in range(300)]) for t in range(30)]
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, result(links=links, tables=tables))
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert "lnk049" in html and "lnk050" not in html
    assert html.count('class="doc-table"') == 20
    assert "h&lt;1&gt;" in html and "r199t0<" in html and "r200t0<" not in html


async def test_per_item_text_is_clipped(app) -> None:
    big = "x" * 10_000_000
    jid = await run_job(
        app,
        JobType.SEARCH_READ,
        SEARCH_REQ,
        result(
            tables=[Table(caption=big, headers=[big], rows=[[big]])],
            warnings=[big],
            links=[Link(url="https://l.example/", text=big, external=True)],
        ),
    )
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert len(html) < 100_000
    assert "x" * 2001 not in html and "x" * 1990 in html


async def test_status_poll_stops_when_job_is_gone(app) -> None:
    async with logged_in(app) as c:
        r = await c.get("/gui/partials/job/" + "0" * 32 + "/status?live=1")
    assert r.status_code == 286  # htmx: swap, then stop polling
    assert "no longer exists" in r.text and "hx-trigger" not in r.text


async def test_json_ld_rendered_as_tree(app) -> None:
    from research_engine_client.models import StructuredData

    sd = StructuredData(json_ld=[{"@type": "Article", "name": "<u>n</u>"}])
    jid = await run_job(app, JobType.SEARCH_READ, SEARCH_REQ, result(structured_data=sd))
    async with logged_in(app) as c:
        html = (await c.get(f"/jobs/{jid}")).text
    assert "&lt;u&gt;n&lt;/u&gt;" in html and "@type" in html


async def test_job_stream_is_filtered_to_the_job(app, live_server: str) -> None:
    from .test_gui_dashboard import _read_until, _subscribed, live_client, log_texts

    bus = app.state.services.events
    before = bus.subscriber_count
    async with (
        live_client(app, live_server) as c,
        c.stream("GET", "/gui/events/stream?job=abc123") as resp,
    ):
        await _subscribed(app, before)
        await Emitter(bus, "ffff").info(EventKind.FETCH_DONE, "other-job")
        await Emitter(bus, "abc123").info(EventKind.FETCH_DONE, "mine")
        buf = await asyncio.wait_for(_read_until(resp, "mine"), 2.0)
    assert any("mine" in t for t in log_texts(buf)) and "other-job" not in buf
