"""SearchProvider seam (V1-01). Concrete providers live in sibling modules."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from research_engine_client.models import Infobox, TimeRange, UnresponsiveEngine


@dataclass
class RawHit:
    url: str
    title: str
    content: str
    engines: list[str]
    positions: list[int]  # already offset by (pageno-1)*10
    published: datetime | None


@dataclass
class RawSearchPage:
    hits: list[RawHit]
    suggestions: list[str]
    infoboxes: list[Infobox]
    unresponsive: list[UnresponsiveEngine]


class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        categories: list[str],
        engines: list[str],
        language: str,
        time_range: TimeRange | None,
        pageno: int,
    ) -> RawSearchPage: ...

    async def health(self) -> bool: ...
