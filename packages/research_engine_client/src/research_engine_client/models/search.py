"""Search request/response models (V1-01..V1-03)."""

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, StringConstraints

from ._base import Model, RequestModel

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class SearchIntent(StrEnum):
    GENERAL = "general"
    TECHNICAL = "technical"
    LIBRARY = "library"
    PRODUCT = "product"
    STANDARD = "standard"
    NEWS = "news"
    ACADEMIC = "academic"


class SearchDepth(StrEnum):
    """How many SearXNG result pages to request: quick=1, standard=2, deep=3."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class TimeRange(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class SearchRequest(RequestModel):
    query: NonBlank
    intent: SearchIntent = SearchIntent.GENERAL
    max_results: int = Field(default=20, ge=1, le=100)
    time_range: TimeRange | None = None
    language: str = Field(default="en-GB", pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$|^all$")
    engines: tuple[str, ...] | None = Field(
        default=None, description="Override the intent preset's engines"
    )
    depth: SearchDepth = SearchDepth.STANDARD
    use_cache: bool = True


class SearchResult(Model):
    rank: int = Field(ge=1)
    url: str
    canonical_url: str
    title: str
    snippet: str
    domain: str
    engines: list[str]
    score: float = Field(ge=0)
    published_at: AwareDatetime | None = None


class InfoboxLink(Model):
    title: str
    url: str


class Infobox(Model):
    title: str
    content: str | None = None
    engine: str | None = None
    urls: list[InfoboxLink] = Field(default_factory=list[InfoboxLink])


class UnresponsiveEngine(Model):
    engine: str
    error: str


class SearchResponse(Model):
    query: str
    results: list[SearchResult] = Field(default_factory=list[SearchResult])
    suggestions: list[str] = Field(default_factory=list[str])
    infoboxes: list[Infobox] = Field(default_factory=list[Infobox])
    unresponsive_engines: list[UnresponsiveEngine] = Field(default_factory=list[UnresponsiveEngine])


class IntentPreset(Model):
    categories: list[str]
    engines: list[str]
    description: str


class EngineInfo(Model):
    name: str
    used_by: list[SearchIntent]


class EnginesResponse(Model):
    engines: list[EngineInfo]
    intents: dict[SearchIntent, IntentPreset]


__all__ = [
    "EngineInfo",
    "EnginesResponse",
    "Infobox",
    "InfoboxLink",
    "IntentPreset",
    "SearchDepth",
    "SearchIntent",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "TimeRange",
    "UnresponsiveEngine",
]
