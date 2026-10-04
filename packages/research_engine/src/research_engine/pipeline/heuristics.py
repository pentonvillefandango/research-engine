"""Signals that a page needs a real browser (V1-04 escalation)."""

import re

_NOSCRIPT_JS = re.compile(
    r"<noscript[^>]*>[^<]*(enable|requires?|need)[^<]*javascript", re.IGNORECASE
)
_EMPTY_MOUNT = re.compile(
    r"""<div[^>]+id=["'](root|app|__next|__nuxt|svelte|main)["'][^>]*>\s*</div>""",
    re.IGNORECASE,
)
_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)


def looks_js_rendered(html: str, *, word_count: int, threshold: int) -> bool:
    """True when the static HTML looks like an empty shell for a client-side app."""
    if _NOSCRIPT_JS.search(html):
        return True
    return (
        bool(_EMPTY_MOUNT.search(html))
        and len(_SCRIPT.findall(html)) >= 3
        and (word_count < threshold)
    )
