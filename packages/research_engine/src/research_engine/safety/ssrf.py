"""SSRF protection (§8): block fetches to non-public addresses unless allow-listed.

Residual risk: DNS can change between this check and the connection (rebinding).
Mitigated by checking every redirect hop and by Crawl4AI's own internal-URL guard; see ADR-0026.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

import httpx
from research_engine_client.models import ErrorCode

from research_engine.errors import ServiceError

Resolver = Callable[[str], Awaitable[list[str]]]

_EXTRA_BLOCKED = [
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


async def _system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_blocked_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:  # NAT64 embeds an IPv4 address in the low 32 bits
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return (
        ip.is_private  # includes 0.0.0.0/8
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or any(ip in n for n in _EXTRA_BLOCKED)
    )


def _host_of(url: httpx.URL) -> str:
    """The exact host httpx will connect to (IDNA-2008 punycode, lower-case, no brackets/dot)."""
    return url.raw_host.decode("ascii").lower().rstrip(".")


def normalize_host(host: str) -> str:
    """Normalise an allow-list entry with the same parser the fetchers use (httpx.URL).

    Raises ValueError if the entry is not a valid host.
    """
    host = host.strip()
    candidates = [f"http://{host}/"]
    if ":" in host and not host.startswith("["):
        candidates.append(f"http://[{host}]/")
    for candidate in candidates:
        try:
            return _host_of(httpx.URL(candidate))
        except httpx.InvalidURL:
            continue
    raise ValueError(f"invalid host {host!r}")


def _legacy_ipv4(host: str) -> str | None:
    """Canonicalise numeric hosts such as 127.1, 0x7f.1, 2130706433 (the resolver would too)."""
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


class SsrfGuard:
    def __init__(self, allow_hosts: frozenset[str], resolver: Resolver | None = None) -> None:
        hosts: set[str] = set()
        for h in allow_hosts:
            try:
                hosts.add(normalize_host(h))
            except ValueError:
                continue  # an unusable allow-list entry can never match
        self._allow = frozenset(hosts)
        self._resolve = resolver or _system_resolve

    @staticmethod
    def _blocked(url: object, why: str) -> ServiceError:
        return ServiceError.of(
            ErrorCode.SSRF_BLOCKED,
            f"blocked: {why}",
            retryable=False,
            source=str(url),
            http_status=403,
        )

    async def check(self, url: str | httpx.URL) -> httpx.URL:
        """Validate ``url`` and return the parsed, userinfo-free URL to connect to.

        The returned object must be what the caller requests: the host that was checked is then
        the host that is connected to (one parser, no differential).
        """
        try:
            parsed = url if isinstance(url, httpx.URL) else httpx.URL(url)
            parsed.port  # noqa: B018 - validates the port
            host = _host_of(parsed)
        except (httpx.InvalidURL, UnicodeError) as exc:
            raise self._blocked(url, "unparseable URL") from exc
        if parsed.scheme not in ("http", "https"):
            raise self._blocked(url, f"scheme {parsed.scheme!r} not allowed")
        if not host:
            raise self._blocked(url, "missing host")
        safe = parsed.copy_with(userinfo=b"") if parsed.userinfo else parsed
        if host in self._allow:
            return safe
        if host == "localhost" or host.endswith(".localhost"):
            raise self._blocked(url, "localhost")
        try:
            addresses = [str(ipaddress.ip_address(host.split("%", 1)[0]))]
        except ValueError:
            legacy = _legacy_ipv4(host)
            if legacy is not None:
                addresses = [legacy]
            else:
                try:
                    addresses = await self._resolve(host)
                except OSError as exc:
                    raise ServiceError.of(
                        ErrorCode.FETCH_FAILED,
                        f"DNS lookup failed for {host}: {exc}",
                        retryable=True,
                        source=str(url),
                    ) from exc
        bad = [a for a in addresses if _is_blocked_ip(a)]
        if bad:
            raise self._blocked(url, f"{host} resolves to non-public address {bad[0]}")
        return safe
