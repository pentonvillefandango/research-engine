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
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://research.localhost",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c
