"""The GUI's "Copy as Python" snippet runs against the real app through the real client (V1-21).

Locks ``try.js`` ``snippets()`` and ``ResearchEngineClient`` together: the generated code is
executed verbatim (only ``asyncio.run`` is intercepted so it runs on the test's event loop).
"""

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest
import research_engine_client
from research_engine_client.models import Document, JobDetail, SearchResponse

from .test_gui_try import NODE, snippets

CASES: list[tuple[str, dict[str, Any], type[Any]]] = [
    ("search", {"query": "python"}, SearchResponse),
    ("fetch", {"url": "https://blog.example/post"}, Document),
    ("search_read", {"search": {"query": "python"}, "top_n": 1}, JobDetail),
]


@pytest.mark.parametrize(("kind", "request_", "model"), CASES)
async def test_python_snippet_runs(
    kind: str,
    request_: dict[str, Any],
    model: type[Any],
    app,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if NODE is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI: the copy-as snippet tests need it")
        pytest.skip("node is not installed")
    code = snippets(kind, request_)["python"]

    real = research_engine_client.ResearchEngineClient

    class AsgiClient(real):  # the snippet only knows (url, api_key=...)
        def __init__(self, base_url: str, api_key: str, **kw: Any) -> None:
            super().__init__(base_url, api_key, transport=httpx.ASGITransport(app=app), **kw)

    monkeypatch.setattr(research_engine_client, "ResearchEngineClient", AsgiClient)
    monkeypatch.setenv("RESEARCH_ENGINE_URL", "http://research.localhost")
    monkeypatch.setenv("RESEARCH_ENGINE_API_KEY", "test-key")
    pending: list[Coroutine[Any, Any, None]] = []
    monkeypatch.setattr(asyncio, "run", lambda coro: pending.append(coro))

    exec(compile(code, f"<copy-as-python:{kind}>", "exec"), {"__name__": "__main__"})  # noqa: S102
    assert len(pending) == 1
    await pending[0]

    # the app's logging may share stdout; the snippet's own output is the pretty JSON from "{"
    lines = capsys.readouterr().out.splitlines()
    result = model.model_validate_json("\n".join(lines[lines.index("{") :]))
    if model is JobDetail:
        assert result.job.status.is_terminal and result.result is not None
    elif model is Document:
        assert result.status == 200 and result.markdown
    else:
        assert result.query == "python" and result.results
