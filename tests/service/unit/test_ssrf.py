import httpx
import pytest
from research_engine.errors import ServiceError
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode


def resolver(table: dict[str, list[str]]):
    async def _resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]

    return _resolve


GUARD = SsrfGuard(
    frozenset({"intranet.example", "Bücher.Example"}),
    resolver=resolver(
        {
            "public.example": ["93.184.216.34"],
            "rebind.example": ["93.184.216.34", "10.0.0.5"],
            "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
            "mapped.example": ["::ffff:127.0.0.1"],
            "nat64.example": ["64:ff9b::a00:1"],
            "nat64pub.example": ["64:ff9b::5db8:d822"],
            "intranet.example": ["10.1.2.3"],
            "xn--bcher-kva.example": ["10.9.9.9"],
        }
    ),
)


async def test_public_allowed() -> None:
    await GUARD.check("https://public.example/page")
    await GUARD.check("https://v6.example/")
    await GUARD.check("https://public.example./page")  # trailing dot
    await GUARD.check("https://nat64pub.example/")  # NAT64 of a public v4


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]:8080/x",
        "http://10.0.0.1/",
        "http://172.16.5.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://0.1.2.3/",
        "http://[::]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://224.0.0.1/",
        "http://[::ffff:10.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://localhost:8000/",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "https://rebind.example/",
        "https://mapped.example/",
        "https://nat64.example/",
        "ftp://public.example/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///nohost",
        "http://[::1/",  # unparseable
        "http://0x7f.1/",  # legacy IPv4 forms: the guard must not rely on the resolver
        "http://2130706433/",
        "http://127.1/",
        "http://0/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://0xa9.0xfe.0xa9.0xfe/",
        "http://[fec0::1]/",  # deprecated site-local
        "http://[64:ff9b:1::1]/",  # local-use NAT64
        "http://a\u200db.example/",  # invalid IDNA under httpx's parser
    ],
)
async def test_blocked(url: str) -> None:
    with pytest.raises(ServiceError) as ei:
        await GUARD.check(url)
    d = ei.value.detail
    assert d.code is ErrorCode.SSRF_BLOCKED and d.retryable is False
    assert ei.value.http_status == 403 and d.source == url


async def test_allow_list_bypasses() -> None:
    await GUARD.check("http://Intranet.Example/wiki")
    await GUARD.check("http://intranet.example./wiki")  # trailing dot, same host


async def test_allow_list_idn_and_punycode_forms_match() -> None:
    await GUARD.check("http://bücher.example/")
    await GUARD.check("http://BÜCHER.example/")
    await GUARD.check("http://xn--bcher-kva.example/")


async def test_allow_list_ipv6_literal() -> None:
    g = SsrfGuard(frozenset({"[::1]"}), resolver=resolver({}))
    await g.check("http://[::1]/")  # allow-listed as "[::1]"; no DNS involved


async def test_ip_literals_never_hit_dns() -> None:
    async def boom(host: str) -> list[str]:
        raise AssertionError("DNS used for literal")

    g = SsrfGuard(frozenset(), resolver=boom)
    with pytest.raises(ServiceError):
        await g.check("http://[fd00::1]/")
    with pytest.raises(ServiceError):
        await g.check("http://127.0.0.1/")


async def test_dns_failure_is_fetch_failed() -> None:
    with pytest.raises(ServiceError) as ei:
        await GUARD.check("https://nope.example/")
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable


# Legacy numeric hosts must be blocked even when the resolver would answer "public".
LEGACY_GUARD = SsrfGuard(frozenset(), resolver=resolver({}))


async def _public_for_anything(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    ["http://0x7f.1/", "http://2130706433/", "http://127.1/", "http://0/", "http://0x7f000001/"],
)
async def test_legacy_ipv4_blocked_even_with_public_resolver(url: str) -> None:
    g = SsrfGuard(frozenset(), resolver=_public_for_anything)
    with pytest.raises(ServiceError) as ei:
        await g.check(url)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


# IDNA-2003 (Python codec) vs IDNA-2008 (httpx) differential: the guard must check the very
# host httpx will connect to.
@pytest.mark.parametrize(
    ("host", "puny"),
    [
        ("straße.example", "xn--strae-oqa.example"),
        ("ς.example", "xn--3xa.example"),
        ("faß.de", "xn--fa-hia.de"),
    ],
)
async def test_resolver_receives_idna2008_host(host: str, puny: str) -> None:
    seen: list[str] = []

    async def rec(h: str) -> list[str]:
        seen.append(h)
        return ["10.0.0.9"]  # only the 2008 form resolves private in this mapping

    g = SsrfGuard(frozenset(), resolver=rec)
    with pytest.raises(ServiceError) as ei:
        await g.check(f"http://{host}/")
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED
    assert seen == [puny]


async def test_only_2008_form_private_is_blocked() -> None:
    table = {
        "strasse.example": ["93.184.216.34"],  # what the 2003 codec would have looked up
        "xn--strae-oqa.example": ["10.0.0.9"],  # what httpx connects to
    }
    g = SsrfGuard(frozenset(), resolver=resolver(table))
    with pytest.raises(ServiceError) as ei:
        await g.check("http://straße.example/")
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


async def test_check_returns_the_url_that_is_connected_to() -> None:
    g = SsrfGuard(frozenset(), resolver=resolver({"xn--strae-oqa.example": ["93.184.216.34"]}))
    u = await g.check("http://u:p@straße.example:8080/a?b=1#f")
    assert isinstance(u, httpx.URL)
    assert u.raw_host == b"xn--strae-oqa.example" and u.port == 8080
    assert u.userinfo == b""  # userinfo stripped so httpx cannot turn it into Basic auth


async def test_allow_list_uses_same_parser() -> None:
    g = SsrfGuard(frozenset({"straße.example"}), resolver=resolver({}))
    await g.check("http://xn--strae-oqa.example/")
    await g.check("http://straße.example/")
    g2 = SsrfGuard(frozenset({"strasse.example"}), resolver=resolver({}))
    with pytest.raises(ServiceError):  # 2003 folding must not make this match
        await g2.check("http://straße.example/")
