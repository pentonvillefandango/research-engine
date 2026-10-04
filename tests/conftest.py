"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

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
