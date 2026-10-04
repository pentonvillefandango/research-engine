"""Fetch acceptance against the live dev stack (Crawl4AI) and the public web."""

import os
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from research_engine.app import build_fetch_service
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.fetch import FetchService
from research_engine.safety.http import make_fetch_client
from research_engine_client.models import FetchMethod, FetchRequest

from .live_urls import PDF, SPA, STATIC_ARTICLE

pytestmark = pytest.mark.integration

# Read at import: the settings_env fixture replaces CRAWL4AI_API_TOKEN with a test value.
_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "")
_HTML_TAG = re.compile(r"<(script|style|div|span|html|body)\b", re.IGNORECASE)


@pytest.fixture
async def service(settings_env: None) -> AsyncIterator[FetchService]:
    settings = Settings().model_copy(  # type: ignore[call-arg]
        update={
            "crawl4ai_url": os.environ["CRAWL4AI_LIVE_URL"],
            "crawl4ai_api_token": SecretStr(_TOKEN),
        }
    )
    async with httpx.AsyncClient() as http, make_fetch_client(settings.user_agent) as fetch_http:
        yield build_fetch_service(
            settings,
            http=http,
            fetch_http=fetch_http,
            cache=InMemoryCache(),
            events=InMemoryEventBus(),
        )


def _assert_clean(markdown: str) -> None:
    assert not _HTML_TAG.search(markdown), markdown[:500]


async def test_static_article(service: FetchService) -> None:
    doc, _ = await service.fetch(FetchRequest(url=STATIC_ARTICLE, use_cache=False))
    print(f"STATIC words={doc.word_count} method={doc.provenance.method} warnings={doc.warnings}")
    assert doc.provenance.method is FetchMethod.STATIC
    assert doc.quality.escalation_reason is None
    assert doc.word_count >= 100
    assert "coroutines and tasks" in doc.markdown.lower()  # live heading: "Coroutines and tasks"
    _assert_clean(doc.markdown)


async def test_spa_escalates_to_browser(service: FetchService) -> None:
    doc, _ = await service.fetch(FetchRequest(url=SPA, use_cache=False))
    reason = doc.quality.escalation_reason
    print(f"SPA words={doc.word_count} method={doc.provenance.method} reason={reason}")
    assert doc.provenance.method is FetchMethod.BROWSER, doc.warnings
    assert doc.word_count >= 100
    assert reason is not None and f"escalated to browser: {reason.value}" in doc.warnings
    assert "Albert Einstein" in doc.markdown  # author of the first quote on the rendered page
    _assert_clean(doc.markdown)


async def test_pdf(service: FetchService) -> None:
    doc, _ = await service.fetch(FetchRequest(url=PDF, use_cache=False))
    print(f"PDF words={doc.word_count} method={doc.provenance.method} warnings={doc.warnings}")
    assert doc.provenance.method is FetchMethod.STATIC
    assert doc.quality.escalation_reason is None
    assert doc.word_count >= 100
    assert "HTTP Semantics" in doc.markdown
    _assert_clean(doc.markdown)
