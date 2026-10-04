from research_engine.cache.base import cache_key
from research_engine.cache.memory import InMemoryCache
from research_engine_client.models import SearchRequest
from research_engine_client.models.jobs import FetchRequestOptions, SearchReadRequest


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_ttl_and_stats() -> None:
    clock = Clock()
    cache = InMemoryCache(clock=clock)
    assert await cache.get("k") is None
    await cache.set("k", b"v", ttl_s=10)
    assert await cache.get("k") == b"v"
    clock.t += 11
    assert await cache.get("k") is None
    s = cache.stats()
    assert (s.hits, s.misses) == (1, 2)


def test_cache_key_stable() -> None:
    a = cache_key("search", SearchRequest(query="x", engines=("b", "a")))
    b = cache_key("search", SearchRequest(query="x", engines=("b", "a")))
    assert a == b and len(a) == 64
    assert cache_key("fetch", "https://a.example") != cache_key("search", "https://a.example")


def test_cache_key_ignores_use_cache_flag() -> None:
    assert cache_key("search", SearchRequest(query="x", use_cache=False)) == cache_key(
        "search", SearchRequest(query="x", use_cache=True)
    )


def test_cache_key_ignores_nested_use_cache() -> None:
    def key(flag: bool) -> str:
        return cache_key(
            "search_read",
            SearchReadRequest(
                search=SearchRequest(query="x", use_cache=flag),
                fetch=FetchRequestOptions(use_cache=flag),
            ),
        )

    assert key(True) == key(False)
    other = SearchReadRequest(search=SearchRequest(query="y"))
    assert cache_key("search_read", other) != key(True)
