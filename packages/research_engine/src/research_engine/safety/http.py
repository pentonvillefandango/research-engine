"""Outbound request hygiene shared by the robots and static fetchers (§8)."""

import httpx


def build_clean_request(url: str, headers: dict[str, str], timeout_s: float) -> httpx.Request:
    """Build a bare GET carrying only the given headers.

    Deliberately not ``client.build_request``: that would merge the shared client's cookie jar
    and default headers (including any auth) into a request bound for an untrusted site. Send it
    with ``client.send(request, auth=None)`` so client-level auth is skipped too.
    """
    return httpx.Request(
        "GET",
        url,
        headers={"Accept-Encoding": "gzip, deflate", **headers},
        extensions={"timeout": httpx.Timeout(timeout_s).as_dict()},
    )
