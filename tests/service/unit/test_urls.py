import pytest
from research_engine.pipeline.urls import canonicalize_url, domain_of


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
