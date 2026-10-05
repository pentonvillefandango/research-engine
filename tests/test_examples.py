"""The runnable examples: they import cleanly, skip or fail politely without their env vars,
never print a key, and the client example works end to end against the test app."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from research_engine.testing import STATIC_PAGES
from research_engine_client import ResearchEngineClient

EX = Path(__file__).resolve().parents[1] / "examples"
SECRET = "sk-test-secret-value-123"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EX / f"{name}.py")
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "RESEARCH_ENGINE_API_KEY",
        "RESEARCH_ENGINE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_client_example_imports() -> None:
    assert hasattr(_load("client_usage"), "main")


def test_agent_example_imports() -> None:
    assert hasattr(_load("openai_agents_mcp"), "main")


async def test_agent_example_skips_without_openai_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RESEARCH_ENGINE_API_KEY", SECRET)
    rc = await _load("openai_agents_mcp").main()
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "skipped: OPENAI_API_KEY not set"
    assert SECRET not in out.out + out.err


async def test_agent_skip_takes_precedence_over_missing_engine_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = await _load("openai_agents_mcp").main()
    assert rc == 0
    assert capsys.readouterr().out.strip() == "skipped: OPENAI_API_KEY not set"


async def test_agent_example_needs_engine_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    rc = await _load("openai_agents_mcp").main()
    out = capsys.readouterr()
    assert rc != 0
    assert "RESEARCH_ENGINE_API_KEY" in out.err
    assert SECRET not in out.out + out.err


def test_agent_builds_without_hardcoded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load("openai_agents_mcp")
    server = mod.make_server("https://h.example", "k")
    assert server.params["url"] == "https://h.example/mcp"
    assert server.params["headers"] == {"X-API-Key": "k"}
    assert "model" not in mod.agent_kwargs(None)
    assert mod.agent_kwargs("some-model")["model"] == "some-model"
    assert "untrusted" in mod.INSTRUCTIONS.lower()
    assert "url" in mod.INSTRUCTIONS.lower()


async def test_client_example_needs_key(capsys: pytest.CaptureFixture[str]) -> None:
    rc = await _load("client_usage").main()
    assert rc != 0
    assert "RESEARCH_ENGINE_API_KEY" in capsys.readouterr().err


def _inject(monkeypatch: pytest.MonkeyPatch, mod: ModuleType, transport: Any) -> None:
    def factory(base_url: str, api_key: str, **kw: Any) -> ResearchEngineClient:
        return ResearchEngineClient(base_url, api_key, transport=transport, **kw)

    monkeypatch.setattr(mod, "ResearchEngineClient", factory)


async def test_client_example_runs_against_app(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fake search provider returns result<i>.example URLs; make the fake fetcher serve them.
    for i in range(10):
        monkeypatch.setitem(STATIC_PAGES, f"https://result{i}.example/1", "article.html")
    mod = _load("client_usage")
    _inject(monkeypatch, mod, httpx.ASGITransport(app=app))
    monkeypatch.setenv("RESEARCH_ENGINE_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_ENGINE_URL", "http://research.localhost")
    rc = await mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "title:" in out
    assert "words:" in out
    assert "provenance:" in out
    assert "sha256:" in out
    assert "test-key" not in out


@pytest.mark.parametrize("kind", ["http", "transport"])
async def test_client_example_error_path_hides_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "transport":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(500, text="oops")

    mod = _load("client_usage")
    _inject(monkeypatch, mod, httpx.MockTransport(handler))
    monkeypatch.setenv("RESEARCH_ENGINE_API_KEY", SECRET)
    rc = await mod.main()
    out = capsys.readouterr()
    assert rc != 0
    assert "error" in out.err.lower()
    assert SECRET not in out.out + out.err
