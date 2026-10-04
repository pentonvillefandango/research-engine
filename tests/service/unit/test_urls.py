import pytest
from research_engine.pipeline.urls import canonicalize_url, domain_of, fetch_cache_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Path/?b=2&a=1#frag", "https://example.com/Path?a=1&b=2"),
        ("https://example.com:443/x/", "https://example.com/x"),
        ("http://example.com:80/", "http://example.com/"),
        (
            "https://example.com/?utm_source=x&utm_medium=y&id=3&gclid=z",
            "https://example.com/?id=3",
        ),
        ("https://example.com/a?ref=hn&fbclid=1", "https://example.com/a"),
        ("https://www.example.com/a", "https://www.example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("http://example.com/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_domain_of_strips_www() -> None:
    assert domain_of("https://WWW.Example.com/x") == "example.com"


@pytest.mark.parametrize("raw", ["http://[::1", "http://a:99999/", "http://a:abc/", "", "   "])
def test_hostile_urls_never_raise(raw: str) -> None:
    assert canonicalize_url(raw) == raw.strip()
    assert domain_of(raw) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://[::1]:80/x", "http://[::1]/x"),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("https://[2001:DB8::1]/", "https://[2001:db8::1]/"),
    ],
)
def test_ipv6_hosts_keep_brackets(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM:443/Path/?b=2&a=1", "https://example.com/Path/?b=2&a=1"),
        ("http://example.com:80/docs", "http://example.com/docs"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com", "https://example.com/"),
        ("https://g.example/r?ref=main", "https://g.example/r?ref=main"),
        ("https://e.example/a?utm_source=x&id=3&UTM_medium=y", "https://e.example/a?id=3"),
        (
            "https://e.example/a?gclid=1&fbclid=2&msclkid=3&mc_cid=4&mc_eid=5&igshid=6&yclid=7",
            "https://e.example/a",
        ),
        ("https://e.example/a?x=%2F&y=a+b", "https://e.example/a?x=%2F&y=a+b"),
        ("https://e.example/a#section", "https://e.example/a"),
        ("https://e.example/#/pricing", "https://e.example/#/pricing"),
        ("https://e.example/#!/about", "https://e.example/#!/about"),
        ("https://[::1]:8080/a", "https://[::1]:8080/a"),
        ("https://user:pw@e.example/a", "https://e.example/a"),
    ],
)
def test_fetch_cache_url(raw: str, expected: str) -> None:
    assert fetch_cache_url(raw) == expected


@pytest.mark.parametrize("raw", ["http://[::1", "http://a:99999/", "", "   "])
def test_fetch_cache_url_is_total(raw: str) -> None:
    assert isinstance(fetch_cache_url(raw), str)
