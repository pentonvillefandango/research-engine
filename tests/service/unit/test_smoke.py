"""`research-engine smoke` (V1-22, V1-21): the demo set against the API through the typed client,
plus an MCP ``initialize`` with ``Host: <SITE_HOST>``."""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from research_engine import smoke, testing
from research_engine.adapters.search import RawSearchPage
from research_engine.cli import main
from research_engine.config_files import Demo, load_demos
from research_engine.errors import ServiceError
from research_engine_client import ResearchEngineClient
from research_engine_client.models import ErrorCode

ROOT = Path(__file__).resolve().parents[3]
CHECKS = ["version", "health", "mcp", "search", "fetch_static", "fetch_pdf", "search_read"]

# The real demo ids with URLs the fake fetchers serve.
FAKE_DEMOS: list[dict[str, Any]] = [
    {
        "id": "vector-dbs",
        "kind": "search",
        "request": {"query": "Compare open-source vector databases", "intent": "technical"},
    },
    {
        "id": "vector-dbs-read",
        "kind": "search_read",
        "request": {
            "search": {"query": "Compare open-source vector databases", "intent": "technical"},
            "top_n": 5,
        },
    },
    {
        "id": "asyncio-taskgroup",
        "kind": "fetch",
        "request": {"url": "https://blog.example/post", "mode": "static"},
    },
    {"id": "vendor-pdf", "kind": "fetch", "request": {"url": "https://docs.example/sample.pdf"}},
]


def demos() -> list[Demo]:
    return [Demo.model_validate({"label": d["id"], "description": "d", **d}) for d in FAKE_DEMOS]


@pytest.fixture(autouse=True)
def served_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake search hits point at pages the fake fetcher serves, so search_read reads documents."""
    for i in range(1, 13):
        monkeypatch.setitem(testing.STATIC_PAGES, f"https://result{i}.example/1", "article.html")
    # the fixture PDF (tests/fixtures/pages/sample.pdf) has only 27 words
    monkeypatch.setattr(smoke, "MIN_WORDS", 20)


@pytest.fixture
async def rc(app: FastAPI) -> AsyncIterator[ResearchEngineClient]:
    transport = httpx.ASGITransport(app=app)
    async with ResearchEngineClient(
        "http://research.localhost", "test-key", transport=transport
    ) as c:
        yield c


@pytest.fixture
async def mcp_http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c


def by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in result["checks"]}


async def test_all_checks_pass(rc: ResearchEngineClient, mcp_http: httpx.AsyncClient) -> None:
    result = await smoke.run_smoke(rc, demos(), mcp_http=mcp_http, site_host="research.localhost")
    assert [c["name"] for c in result["checks"]] == CHECKS
    assert result["ok"] is True, result
    for c in result["checks"]:
        assert set(c) == {"name", "ok", "ms", "detail"} and c["ok"] is True
        assert isinstance(c["ms"], int) and c["ms"] >= 0
    assert isinstance(result["took_ms"], int)
    json.dumps(result)  # serialisable as one JSON line


async def test_every_check_bypasses_the_cache(
    app: FastAPI, rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    seen: list[dict[str, Any]] = []
    real = rc._http.request  # pyright: ignore[reportPrivateUsage]

    async def spy(method: str, url: str, **kw: Any) -> httpx.Response:
        if kw.get("content"):
            seen.append(json.loads(kw["content"]))
        return await real(method, url, **kw)

    rc._http.request = spy  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    result = await smoke.run_smoke(rc, demos(), mcp_http=mcp_http, site_host="research.localhost")
    assert result["ok"] is True, result
    assert len(seen) == 4
    for body in seen:
        if "search" in body:  # search_read
            assert body["search"]["use_cache"] is False and body["fetch"]["use_cache"] is False
            assert body["top_n"] == 2
        else:
            assert body["use_cache"] is False


async def test_search_failure_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    async def broken(self: object, query: str, **kw: object) -> object:
        raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "searxng down", retryable=True)

    monkeypatch.setattr(testing.FakeSearchProvider, "search", broken)
    result = await smoke.run_smoke(rc, demos(), mcp_http=mcp_http, site_host="research.localhost")
    checks = by_name(result)
    assert result["ok"] is False
    assert checks["search"]["ok"] is False and checks["search"]["detail"]
    assert checks["fetch_static"]["ok"] is True and checks["version"]["ok"] is True


async def test_too_few_results_fails_search(
    monkeypatch: pytest.MonkeyPatch, rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    real = testing.FakeSearchProvider.search

    async def few(self: testing.FakeSearchProvider, query: str, **kw: Any) -> RawSearchPage:
        page = await real(self, query, **kw)
        return dataclasses.replace(page, hits=page.hits[:3] if kw["pageno"] == 1 else [])

    monkeypatch.setattr(testing.FakeSearchProvider, "search", few)
    result = await smoke.run_smoke(rc, demos(), mcp_http=mcp_http, site_host="research.localhost")
    assert result["ok"] is False and by_name(result)["search"]["ok"] is False


async def test_short_document_fails_fetch(
    monkeypatch: pytest.MonkeyPatch, rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    monkeypatch.setattr(smoke, "MIN_WORDS", 100)
    result = await smoke.run_smoke(rc, demos(), mcp_http=mcp_http, site_host="research.localhost")
    pdf = by_name(result)["fetch_pdf"]
    assert result["ok"] is False and pdf["ok"] is False and "(< 100)" in pdf["detail"]


async def test_wrong_site_host_fails_mcp(
    rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    result = await smoke.run_smoke(rc, demos(), mcp_http=mcp_http, site_host="evil.example")
    checks = by_name(result)
    assert result["ok"] is False
    assert checks["mcp"]["ok"] is False and "421" in checks["mcp"]["detail"]
    assert all(c["ok"] for n, c in checks.items() if n != "mcp")


async def test_mcp_sends_the_api_key(app: FastAPI, rc: ResearchEngineClient) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as keyless:
        result = await smoke.run_smoke(
            rc, demos(), mcp_http=keyless, site_host="research.localhost"
        )
    assert by_name(result)["mcp"]["ok"] is False and "401" in by_name(result)["mcp"]["detail"]


async def test_missing_demo_fails_its_check(
    rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    ds = [d for d in demos() if d.id != "vendor-pdf"]
    result = await smoke.run_smoke(rc, ds, mcp_http=mcp_http, site_host="research.localhost")
    assert by_name(result)["fetch_pdf"] == {
        "name": "fetch_pdf",
        "ok": False,
        "ms": 0,
        "detail": "no demo 'vendor-pdf'",
    }


async def test_deadline_marks_pending_checks_failed(
    monkeypatch: pytest.MonkeyPatch, rc: ResearchEngineClient, mcp_http: httpx.AsyncClient
) -> None:
    async def slow(self: object, query: str, **kw: object) -> object:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    monkeypatch.setattr(testing.FakeSearchProvider, "search", slow)
    result = await smoke.run_smoke(
        rc, demos(), deadline_s=1.0, mcp_http=mcp_http, site_host="research.localhost"
    )
    checks = by_name(result)
    assert result["ok"] is False and [c["name"] for c in result["checks"]] == CHECKS
    assert checks["health"]["ok"] is True
    for name in ("search", "fetch_static", "fetch_pdf", "search_read"):
        assert checks[name]["ok"] is False and checks[name]["detail"] == "deadline"
    assert result["took_ms"] < 10_000


def test_real_demo_file_has_every_smoke_demo() -> None:
    ids = {d.id for d in load_demos(ROOT / "config" / "demos.yaml")}
    assert set(smoke.SMOKE_DEMOS.values()) <= ids


# --- CLI ------------------------------------------------------------------------------------


@pytest.fixture
def demos_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "demos.yaml"
    path.write_text(
        yaml.safe_dump({"demos": [{"label": d["id"], "description": "d", **d} for d in FAKE_DEMOS]})
    )
    monkeypatch.setenv("DEMOS_FILE", str(path))
    monkeypatch.setenv("SITE_HOST", "research.localhost")
    return path


async def test_cli_prints_one_json_line(
    live_server: str, demos_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await asyncio.to_thread(main, ["smoke", "--url", live_server, "--timeout", "60"])
    out = capsys.readouterr()
    # the in-process test server logs to the same stdout; the CLI's own output is one line
    lines = [ln for ln in out.out.strip().splitlines() if '"command": "smoke"' in ln]
    assert len(lines) == 1 and out.out.strip().splitlines()[-1] == lines[0], out
    result = json.loads(lines[0])
    assert code == 0 and result["ok"] is True, result
    assert [c["name"] for c in result["checks"]] == CHECKS
    assert "test-key" not in out.out + out.err


async def test_cli_wrong_site_host_exits_1(
    live_server: str,
    demos_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SITE_HOST", "evil.example")
    code = await asyncio.to_thread(main, ["smoke", "--url", live_server])
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and result["ok"] is False
    assert by_name(result)["mcp"]["ok"] is False


@pytest.mark.parametrize("missing", ["API_KEY", "SITE_HOST"])
def test_cli_missing_env_is_usage_error(
    demos_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.delenv(missing)
    code = main(["smoke", "--url", "http://127.0.0.1:9"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 2 and len(lines) == 1
    result = json.loads(lines[0])
    assert result["ok"] is False and missing in result["error"]
