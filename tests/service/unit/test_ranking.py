from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.pipeline.ranking import merge_and_score


def hit(url: str, engines: list[str], positions: list[int], title: str = "t") -> RawHit:
    return RawHit(
        url=url, title=title, content="c", engines=engines, positions=positions, published=None
    )


def page(*hits: RawHit) -> RawSearchPage:
    return RawSearchPage(hits=list(hits), suggestions=[], infoboxes=[], unresponsive=[])


def test_agreement_beats_single_top_rank() -> None:
    res = merge_and_score(
        [
            page(
                hit("https://solo.example/", ["a"], [1]),
                hit("https://agreed.example/", ["a", "b", "c"], [5, 6, 7]),
            )
        ],
        10,
    )
    assert [r.domain for r in res] == ["agreed.example", "solo.example"]
    assert res[0].rank == 1 and res[0].engines == ["a", "b", "c"]


def test_merge_across_pages_and_dedupe_canonical() -> None:
    res = merge_and_score(
        [
            page(hit("https://x.example/a/?utm_source=1", ["a"], [2], title="")),
            page(hit("https://x.example/a", ["b"], [12], title="Real title")),
        ],
        10,
    )
    assert len(res) == 1
    assert res[0].engines == ["a", "b"] and res[0].title == "Real title"
    expected = round((1 / 62 + 1 / 72) * 1.1, 6)
    assert res[0].score == expected


def test_truncates_and_ranks() -> None:
    hits = [hit(f"https://e{i}.example/", ["a"], [i]) for i in range(1, 6)]
    res = merge_and_score([page(*hits)], 3)
    assert [r.rank for r in res] == [1, 2, 3]
    assert [r.domain for r in res] == ["e1.example", "e2.example", "e3.example"]


def test_ties_break_on_min_position_then_url() -> None:
    res = merge_and_score(
        [page(hit("https://b.example/", ["a"], [1]), hit("https://a.example/", ["a"], [1]))], 10
    )
    assert [r.domain for r in res] == ["a.example", "b.example"]
