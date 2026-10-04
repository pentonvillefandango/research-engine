"""Outbound request hygiene shared by the robots and static fetchers (§8)."""

import http.cookiejar

import httpx


def make_fetch_client(user_agent: str) -> httpx.AsyncClient:
    """The shared client for fetching third-party pages.

    - never follows redirects (the fetchers do, hop by hop, after the SSRF check);
    - rejects every cookie, so ``Set-Cookie`` from fetched sites is never stored or replayed;
    - ignores proxy environment variables, which could route around the SSRF guard.
    """
    client = httpx.AsyncClient(
        follow_redirects=False, headers={"User-Agent": user_agent}, trust_env=False
    )
    # An empty allow-list rejects every domain. (Set on the client's own jar: httpx copies a
    # jar passed to the constructor into a fresh default-policy one.)
    client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    return client


def build_clean_request(
    url: str | httpx.URL, headers: dict[str, str], timeout_s: float
) -> httpx.Request:
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
