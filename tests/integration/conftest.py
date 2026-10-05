"""Fixtures for the live-stack integration tests."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from research_engine.app import create_app
from research_engine.config import Settings

from tests.conftest import running_lifespan

from .live_token import crawl4ai_token, require_token

# Read at import: the settings_env fixture replaces CRAWL4AI_API_TOKEN with a test value.
# Falls back to .env when unset (never printed).
_LIVE_TOKEN = crawl4ai_token(os.environ)


@pytest.fixture
async def live_app(settings_env: None, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The real app (``create_app()``, real adapters and lifespan) on the dev stack.

    Needs ``SEARXNG_LIVE_URL`` and ``CRAWL4AI_LIVE_URL`` in the environment, and
    ``CRAWL4AI_API_TOKEN`` there or in ``.env`` (only that key is read; never printed). Uses a
    throwaway file database.
    """
    settings = Settings().model_copy(  # type: ignore[call-arg]
        update={
            "searxng_url": os.environ["SEARXNG_LIVE_URL"],
            "crawl4ai_url": os.environ["CRAWL4AI_LIVE_URL"],
            "crawl4ai_api_token": SecretStr(require_token(_LIVE_TOKEN)),
            "db_path": str(tmp_path / "live.sqlite"),
        }
    )
    application = create_app(settings)
    async with running_lifespan(application):  # one task, like uvicorn (see tests/conftest.py)
        yield application


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=live_app),
        base_url="http://research.localhost",
        headers={"X-API-Key": "test-key"},
        timeout=120,
    ) as c:
        yield c
