"""Merge, dedupe and score hits across engines and pages (V1-01)."""

from dataclasses import dataclass, field
from datetime import datetime

from research_engine_client.models import SearchResult

from research_engine.adapters.search import RawSearchPage

from .urls import canonicalize_url, domain_of

RRF_K = 60
AGREEMENT_BONUS = 0.1


@dataclass
class _Acc:
    url: str
    title: str = ""
    snippet: str = ""
    engines: list[str] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    published: datetime | None = None


def merge_and_score(pages: list[RawSearchPage], max_results: int) -> list[SearchResult]:
    acc: dict[str, _Acc] = {}
    for page in pages:
        for h in page.hits:
            canon = canonicalize_url(h.url)
            a = acc.setdefault(canon, _Acc(url=h.url))
            a.title = a.title or h.title
            a.snippet = a.snippet or h.content
            a.published = a.published or h.published
            a.engines.extend(e for e in h.engines if e not in a.engines)
            a.positions.extend(h.positions)
    scored = []
    for canon, a in acc.items():
        base = sum(1 / (RRF_K + p) for p in a.positions)
        score = round(base * (1 + AGREEMENT_BONUS * (max(len(a.engines), 1) - 1)), 6)
        scored.append((score, min(a.positions, default=10**6), canon, a))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [
        SearchResult(
            rank=i,
            url=a.url,
            canonical_url=canon,
            title=a.title,
            snippet=a.snippet,
            domain=domain_of(a.url),
            engines=a.engines,
            score=score,
            published_at=a.published,
        )
        for i, (score, _, canon, a) in enumerate(scored[:max_results], start=1)
    ]
