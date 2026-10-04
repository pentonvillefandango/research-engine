"""Outbound request hygiene shared by the robots and static fetchers (§8)."""

import httpx


def build_clean_request(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> httpx.Request:
    """Build a GET with only the given headers: no cookies, no auth, no client defaults.

    The shared client's cookie jar and default headers must never leak to a target site, so
    anything they inject is stripped after the request is built.
    """
    request = client.build_request("GET", url, headers=headers)
    for name in ("cookie", "authorization", "proxy-authorization"):
        if name in request.headers and name not in {h.lower() for h in headers}:
            del request.headers[name]
    return request
