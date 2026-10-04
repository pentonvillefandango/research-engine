"""SSRF protection (§8): block fetches to non-public addresses unless allow-listed.

Residual risk: DNS can change between this check and the connection (rebinding).
Mitigated by checking every redirect hop and by Crawl4AI's own internal-URL guard; see ADR-0026.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from research_engine_client.models import ErrorCode

from research_engine.errors import ServiceError

Resolver = Callable[[str], Awaitable[list[str]]]

_EXTRA_BLOCKED = [ipaddress.ip_network("100.64.0.0/10")]
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


def normalize_host(host: str) -> str:
    """Lower-case, strip brackets and trailing dot, IDNA-encode. Raises ValueError if invalid."""
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.rstrip(".")
    if not host or host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid host {host!r}") from exc


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
    def _blocked(url: str, why: str) -> ServiceError:
        return ServiceError.of(
            ErrorCode.SSRF_BLOCKED,
            f"blocked: {why}",
            retryable=False,
            source=url,
            http_status=403,
        )

    async def check(self, url: str) -> None:
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - validates the port; raises ValueError if malformed
            scheme = parts.scheme.lower()
            raw_host = parts.hostname or ""
        except ValueError as exc:
            raise self._blocked(url, "unparseable URL") from exc
        if scheme not in ("http", "https"):
            raise self._blocked(url, f"scheme {parts.scheme!r} not allowed")
        try:
            host = normalize_host(raw_host)
        except ValueError as exc:
            raise self._blocked(url, "invalid host") from exc
        if not host:
            raise self._blocked(url, "missing host")
        if host in self._allow:
            return
        if host == "localhost" or host.endswith(".localhost"):
            raise self._blocked(url, "localhost")
        try:
            addresses = [str(ipaddress.ip_address(host.split("%", 1)[0]))]
        except ValueError:
            try:
                addresses = await self._resolve(host)
            except OSError as exc:
                raise ServiceError.of(
                    ErrorCode.FETCH_FAILED,
                    f"DNS lookup failed for {host}: {exc}",
                    retryable=True,
                    source=url,
                ) from exc
        bad = [a for a in addresses if _is_blocked_ip(a)]
        if bad:
            raise self._blocked(url, f"{host} resolves to non-public address {bad[0]}")
