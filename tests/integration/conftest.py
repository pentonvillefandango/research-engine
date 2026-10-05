"""Fixtures for the live-stack integration tests."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

# Read at import: the settings_env fixture replaces CRAWL4AI_API_TOKEN with a test value.
_LIVE_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "")


@pytest.fixture
async def live_app(settings_env: None, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The real app (``create_app()``, real adapters and lifespan) on the dev stack.

    Needs ``SEARXNG_LIVE_URL``, ``CRAWL4AI_LIVE_URL`` and ``CRAWL4AI_API_TOKEN`` in the
    environment; uses a throwaway file database. No ``.env`` values are read or printed.
    """
    # Imported here, like tests/conftest.py: importing the app at collection time changes
    # logging/capture behaviour for unrelated tests.
    from research_engine.app import create_app
    from research_engine.config import Settings

    settings = Settings().model_copy(  # type: ignore[call-arg]
        update={
            "searxng_url": os.environ["SEARXNG_LIVE_URL"],
            "crawl4ai_url": os.environ["CRAWL4AI_LIVE_URL"],
            "crawl4ai_api_token": SecretStr(_LIVE_TOKEN),
            "db_path": str(tmp_path / "live.sqlite"),
        }
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
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
