"""Test-console demos (config/demos.yaml) and their loader (V1-21)."""

from pathlib import Path

import pytest
from research_engine.config_files import load_demos
from research_engine_client.models import FetchRequest, SearchReadRequest, SearchRequest

from ...integration.live_urls import PDF, SPA, STATIC_ARTICLE

ROOT = Path(__file__).resolve().parents[3]
DEMOS = ROOT / "config" / "demos.yaml"
MODELS = {"search": SearchRequest, "fetch": FetchRequest, "search_read": SearchReadRequest}


def test_demos_valid() -> None:
    demos = load_demos(DEMOS)
    assert len(demos) >= 6 and {d.kind for d in demos} == set(MODELS)
    for d in demos:
        MODELS[d.kind].model_validate(d.request)
        assert d.label and d.description
    assert len({d.id for d in demos}) == len(demos)


def test_demos_cover_the_brief() -> None:
    demos = load_demos(DEMOS)

    def find(kind: str, **want: object) -> object:
        for d in demos:
            if d.kind == kind and all(d.request.get(k) == v for k, v in want.items()):
                return d
        raise AssertionError(f"no {kind} demo with {want}")

    find("search", query="Compare open-source vector databases", intent="technical")
    sr = [d for d in demos if d.kind == "search_read"]
    assert any(
        d.request["search"]["query"] == "Compare open-source vector databases"
        and d.request["search"].get("intent") == "technical"
        and d.request["top_n"] == 5
        for d in sr
    )
    find("fetch", url=STATIC_ARTICLE)
    find("fetch", url=SPA, mode="auto")
    find("fetch", url=PDF)
    find("search", intent="news", time_range="month")


def test_demo_urls_match_the_verified_live_urls() -> None:
    urls = {d.request["url"] for d in load_demos(DEMOS) if d.kind == "fetch"}
    assert {STATIC_ARTICLE, SPA, PDF} <= urls


@pytest.mark.parametrize(
    "body",
    [
        "demos: [{id: a, label: A, description: d, kind: nope, request: {query: q}}]",
        "demos: [{id: a, label: A, description: d, kind: search, request: {query: ''}}]",
        "demos: [{id: a, label: A, description: d, kind: fetch, request: {url: 'ftp://x/'}}]",
        "demos: [{id: a, label: A, description: d, kind: search, request: {query: q, x: 1}}]",
        "demos: [{id: a, label: A, description: d, kind: search, request: {query: q}},"
        " {id: a, label: B, description: d, kind: search, request: {query: r}}]",
        "demos: [{id: 'a b', label: A, description: d, kind: search, request: {query: q}}]",
        "nope: 1",
    ],
)
def test_load_demos_rejects_invalid(tmp_path: Path, body: str) -> None:
    path = tmp_path / "demos.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match="invalid demos file"):
        load_demos(path)
