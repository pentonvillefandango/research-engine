"""Service container hung on app.state (composition root fills it)."""

from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request

from research_engine.cache.base import Cache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.search import SearchService


@dataclass
class Services:
    settings: Settings
    intents: IntentRegistry
    events: InMemoryEventBus  # replaced by SqliteEventBus in step 4 (same interface)
    cache: Cache
    search: SearchService
    http: httpx.AsyncClient | None = None
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


def get_services(request: Request) -> Services:
    return request.app.state.services
