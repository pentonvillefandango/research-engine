"""Shared pytest fixtures."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent

TEST_ENV = {
    "API_KEY": "test-key",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "CRAWL4AI_API_TOKEN": "test-crawl-token",
    "SEARXNG_URL": "http://searxng.test:8080",
    "CRAWL4AI_URL": "http://crawl4ai.test:11235",
    "DB_PATH": ":memory:",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Re-apply pyproject's sqlite ``filterwarnings`` entries as per-test marks.

    Command-line ``-W error`` outranks ini ``filterwarnings``, but marks outrank ``-W``; so
    this keeps the (upstream, GC-time) unclosed-sqlite-connection suppression in force under
    ``pytest -W error`` too. pyproject stays the single source of truth.
    """
    sqlite_filters = [f for f in config.getini("filterwarnings") if "sqlite3" in f]
    for item in items:
        for f in sqlite_filters:
            item.add_marker(pytest.mark.filterwarnings(f), append=False)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(ROOT)
    from research_engine.config import Settings, get_settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def app(settings_env: None):
    from research_engine.app import create_app
    from research_engine.config import Settings
    from research_engine.testing import build_test_services

    settings = Settings()  # type: ignore[call-arg]
    services = build_test_services(settings)
    application = create_app(settings, services=services)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def live_server(app) -> AsyncIterator[str]:
    """The test ``app`` served by a real uvicorn on an ephemeral loopback port; yields its base
    URL (``http://127.0.0.1:<port>``).

    Needed for streaming (SSE) tests: httpx's ``ASGITransport`` buffers the whole response body,
    so a never-ending stream never returns there. ``lifespan="off"`` because the ``app`` fixture
    already runs the lifespan. Runs on the test's event loop, so tests can emit events directly.
    """
    import asyncio

    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        log_level="warning",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()  # surface a startup failure
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://research.localhost",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c
