from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from research_engine_client.models.search import (
    SearchDepth,
    SearchIntent,
    SearchRequest,
    SearchResponse,
    SearchResult,
    TimeRange,
    UnresponsiveEngine,
)


def test_search_request_defaults() -> None:
    r = SearchRequest(query="vector databases")
    assert (r.intent, r.max_results, r.language, r.depth) == (
        SearchIntent.GENERAL,
        20,
        "en-GB",
        SearchDepth.STANDARD,
    )
    assert r.time_range is None and r.engines is None and r.use_cache is True


@pytest.mark.parametrize("bad", ["", "   "])
def test_search_request_rejects_blank_query(bad: str) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query=bad)


@pytest.mark.parametrize("n", [0, 101])
def test_max_results_bounds(n: int) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="x", max_results=n)


def test_search_request_hashable() -> None:
    assert hash(SearchRequest(query="x", time_range=TimeRange.WEEK))


def test_intents_cover_requirements() -> None:
    assert {i.value for i in SearchIntent} == {
        "general",
        "technical",
        "library",
        "product",
        "standard",
        "news",
        "academic",
    }


def test_search_response_serialises_nulls() -> None:
    res = SearchResult(
        rank=1,
        url="https://a.example/x",
        canonical_url="https://a.example/x",
        title="t",
        snippet="s",
        domain="a.example",
        engines=["brave"],
        score=0.5,
    )
    resp = SearchResponse(
        query="q",
        results=[res],
        unresponsive_engines=[UnresponsiveEngine(engine="google", error="timeout")],
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["results"][0]["published_at"] is None
    assert dumped["suggestions"] == [] and dumped["infoboxes"] == []


def test_published_at_is_aware() -> None:
    res = SearchResult(
        rank=1,
        url="https://a.example",
        canonical_url="https://a.example",
        title="t",
        snippet="",
        domain="a.example",
        engines=[],
        score=0,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert res.published_at is not None and res.published_at.tzinfo is not None
