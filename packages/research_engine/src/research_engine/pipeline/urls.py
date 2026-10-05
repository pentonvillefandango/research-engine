"""URL canonicalisation for dedupe and cache keys.

All functions here are total: URLs come from third-party search engines, so
malformed input must never raise.
"""

from urllib.parse import SplitResult, parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "igshid",
    "yclid",
    "msclkid",
}
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Click identifiers dropped from page-cache keys. Unlike dedupe, ``ref`` is kept (git branches).
_CLICK_IDS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid"}


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING_EXACT


def _split(url: str) -> tuple[SplitResult, int | None] | None:
    """Split a URL, returning None when it is unparseable (bad IPv6 literal or port)."""
    try:
        parts = urlsplit(url.strip())
        return parts, parts.port
    except ValueError:
        return None


def canonicalize_url(url: str) -> str:
    stripped = url.strip()
    split = _split(stripped)
    if split is None:
        return stripped
    parts, port = split
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme and not host:  # empty or relative: nothing to canonicalise
        return stripped
    if ":" in host:  # IPv6 literal: urlsplit strips the brackets
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
        )
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def domain_of(url: str) -> str:
    split = _split(url)
    if split is None:
        return ""
    host = (split[0].hostname or "").lower()
    return host.removeprefix("www.")


def is_http_url(url: str) -> bool:
    """True for a parseable http(s) URL with a host."""
    split = _split(url)
    return (
        split is not None and split[0].scheme.lower() in _DEFAULT_PORTS and bool(split[0].hostname)
    )


def _is_click_param(pair: str) -> bool:
    k = unquote_plus(pair.split("=", 1)[0]).lower()
    return k.startswith("utm_") or k in _CLICK_IDS


def fetch_cache_url(url: str) -> str:
    """Page-cache key form of a URL: conservative, unlike ``canonicalize_url``.

    Lower-cases scheme and host, drops userinfo, the default port, ``utm_*`` and click-id
    params, and fragments unless they are client-side routes (``#/`` or ``#!``). The path and
    the remaining query (order and encoding) are kept exactly. Total: never raises.
    """
    stripped = url.strip()
    split = _split(stripped)
    if split is None:
        return stripped
    parts, port = split
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return stripped
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    query = "&".join(p for p in parts.query.split("&") if p and not _is_click_param(p))
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    return urlunsplit((scheme, netloc, parts.path or "/", query, fragment))
