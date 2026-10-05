"""Crawl4AI API token for the live-stack tests: the environment first, then the repo's .env.

Only the one key is parsed (like scripts/check_sandbox.sh); the value is never logged or printed.
A leading ``export`` (space or tab), surrounding quotes, an inline `` # comment`` and a CRLF
``\r`` are stripped from the .env value.
Kept out of scripts/dev_urls.sh on purpose: that script prints export lines to stdout.
"""

import re
from collections.abc import Mapping
from pathlib import Path

import pytest

KEY = "CRAWL4AI_API_TOKEN"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_LINE = re.compile(rf"(?:export\s+)?{KEY}=(.*)")


def crawl4ai_token(environ: Mapping[str, str], env_file: Path = ENV_FILE) -> str:
    if environ.get(KEY):
        return environ[KEY]
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        m = _LINE.match(line.strip())  # strip() also drops a CRLF file's trailing \r
        if m:
            return _value(m.group(1).strip())
    return ""


def _value(raw: str) -> str:
    """A quoted value is taken between its quotes (anything after the closing quote, such as a
    comment, is ignored); an unquoted one ends at a whitespace-then-# comment, as in Compose."""
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def require_token(token: str) -> str:
    if not token:
        pytest.fail(
            f"{KEY} is not set in the environment or in .env; the live Crawl4AI tests need it",
            pytrace=False,
        )
    return token
