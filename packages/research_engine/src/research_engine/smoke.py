"""Smoke test of a running Research Engine (V1-22, V1-21): the test-console demo set through the
typed client, plus an MCP ``initialize`` sent with ``Host: <SITE_HOST>``.

Runtime dependencies only (the typed client and httpx): ``ops/smoke.sh`` runs this inside the
app container, which has no dev dependencies. Checks run one after another (light on the host)
under one overall deadline; every request bypasses the cache so the real path is exercised.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from research_engine_client import ResearchEngineClient, ResearchEngineError
from research_engine_client.models import (
    DependencyState,
    FetchRequest,
    JobStatus,
    SearchReadRequest,
    SearchReadResult,
    SearchRequest,
)

from research_engine.config_files import Demo

DEADLINE_S = 110.0
MIN_SEARCH_RESULTS = 5
MIN_WORDS = 100
SEARCH_READ_TOP_N = 2
DETAIL_CHARS = 200

SMOKE_DEMOS = {
    "search": "vector-dbs",
    "fetch_static": "asyncio-taskgroup",
    "fetch_pdf": "vendor-pdf",
    "search_read": "vector-dbs-read",
}
"""Smoke check -> demo id in ``config/demos.yaml``. The JS-app demo is left out: it is slow,
and the integration tests cover it."""

MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "research-engine-smoke", "version": "1"},
    },
}


class CheckFailed(Exception):
    """A check ran but its result is not good enough; the message is the check's detail."""


Check = Callable[[], Awaitable[str]]


def _demo(demos: list[Demo], check: str) -> Demo:
    demo_id = SMOKE_DEMOS[check]
    for d in demos:
        if d.id == demo_id:
            return d
    raise CheckFailed(f"no demo {demo_id!r}")


def _clip(text: str) -> str:
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 3] + "..."


async def run_smoke(
    client: ResearchEngineClient,
    demos: list[Demo],
    deadline_s: float = DEADLINE_S,
    *,
    mcp_http: httpx.AsyncClient,
    site_host: str,
) -> dict[str, Any]:
    """Run every check; returns ``{"ok", "checks": [{"name", "ok", "ms", "detail"}], "took_ms"}``.

    ``mcp_http`` must carry the API key (``X-API-Key``) and point at the same server as
    ``client``. Checks still pending when ``deadline_s`` runs out fail with ``"deadline"``.
    """

    async def version() -> str:
        v = await client.version()
        return f"{v.version} {v.git_sha} schema {v.schema_version}"

    async def health() -> str:
        report = await client.health()
        down = sorted(
            n for n, d in report.dependencies.items() if d.state is not DependencyState.UP
        )
        detail = f"{report.status.value}" + (f" (down: {', '.join(down)})" if down else "")
        if report.status not in (DependencyState.UP, DependencyState.DEGRADED):
            raise CheckFailed(detail)
        return detail

    async def mcp() -> str:
        resp = await mcp_http.post(
            "/mcp",
            json=MCP_INIT,
            headers={"Host": site_host, "Accept": "application/json, text/event-stream"},
        )
        if resp.status_code != 200:
            raise CheckFailed(f"HTTP {resp.status_code}")
        try:
            info = resp.json()["result"]["serverInfo"]
        except (ValueError, KeyError, TypeError):
            raise CheckFailed("initialize: no serverInfo in the response") from None
        return f"initialize ok: {info.get('name')} {info.get('version')}"

    async def search() -> str:
        req = SearchRequest.model_validate({**_demo(demos, "search").request, "use_cache": False})
        resp = await client.search(req)
        detail = f"{len(resp.results)} results"
        if len(resp.results) < MIN_SEARCH_RESULTS:
            raise CheckFailed(f"{detail} (< {MIN_SEARCH_RESULTS})")
        return detail

    async def fetch(check: str) -> str:
        req = FetchRequest.model_validate({**_demo(demos, check).request, "use_cache": False})
        doc = await client.fetch(req)
        detail = f"{doc.word_count} words via {doc.provenance.method.value}"
        if doc.word_count < MIN_WORDS:
            raise CheckFailed(f"{detail} (< {MIN_WORDS})")
        return detail

    async def fetch_static() -> str:
        return await fetch("fetch_static")

    async def fetch_pdf() -> str:
        return await fetch("fetch_pdf")

    async def search_read() -> str:
        raw = _demo(demos, "search_read").request
        req = SearchReadRequest.model_validate(
            {
                **raw,
                "top_n": SEARCH_READ_TOP_N,
                "search": {**raw["search"], "use_cache": False},
                "fetch": {**raw.get("fetch", {}), "use_cache": False},
            }
        )
        job = await client.search_read(req)
        detail = await client.wait_for_job(job.id, timeout_s=deadline_s)
        result = detail.result
        n_docs = len(result.documents) if isinstance(result, SearchReadResult) else 0
        text = f"{detail.job.status.value}, {n_docs} documents"
        if detail.job.status not in (JobStatus.DONE, JobStatus.PARTIAL) or n_docs < 1:
            raise CheckFailed(text)
        return text

    checks: list[tuple[str, Check]] = [
        ("version", version),
        ("health", health),
        ("mcp", mcp),
        ("search", search),
        ("fetch_static", fetch_static),
        ("fetch_pdf", fetch_pdf),
        ("search_read", search_read),
    ]
    results: list[dict[str, Any]] = []
    started = time.monotonic()

    async def guarded(name: str, fn: Check) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            ok, detail = True, await fn()
        except CheckFailed as exc:
            ok, detail = False, str(exc)
        except ResearchEngineError as exc:  # already scrubbed of the API key by the client
            ok, detail = False, str(exc)
        except Exception as exc:  # one broken check never stops the run
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        # a check that never sent a request (missing demo) reports 0 ms
        ms = 0 if not ok and detail.startswith("no demo ") else _ms(t0)
        return {"name": name, "ok": ok, "ms": ms, "detail": _clip(detail)}

    in_flight = started
    try:
        async with asyncio.timeout(deadline_s):
            for name, fn in checks:
                in_flight = time.monotonic()
                results.append(await guarded(name, fn))
    except TimeoutError:
        pass
    for i, (name, _) in enumerate(checks[len(results) :]):
        # the check cut off by the deadline reports its time so far; the rest never started
        ms = _ms(in_flight) if i == 0 else 0
        results.append({"name": name, "ok": False, "ms": ms, "detail": "deadline"})
    return {
        "ok": all(r["ok"] for r in results),
        "checks": results,
        "took_ms": _ms(started),
    }


def _ms(t0: float) -> int:
    return max(0, round((time.monotonic() - t0) * 1000))
