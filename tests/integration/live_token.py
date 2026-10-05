"""Crawl4AI API token for the live-stack tests: the environment first, then the repo's .env.

Only the one key is parsed (like scripts/check_sandbox.sh); the value is never logged or printed.
A leading ``export ``, surrounding quotes and a CRLF ``\r`` are stripped from the .env value.
Kept out of scripts/dev_urls.sh on purpose: that script prints export lines to stdout.
"""

from collections.abc import Mapping
from pathlib import Path

import pytest

KEY = "CRAWL4AI_API_TOKEN"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def crawl4ai_token(environ: Mapping[str, str], env_file: Path = ENV_FILE) -> str:
    if environ.get(KEY):
        return environ[KEY]
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()  # also drops a CRLF file's trailing \r
        line = line.removeprefix("export ").lstrip()
        if line.startswith(f"{KEY}="):
            return _unquote(line.split("=", 1)[1].strip())
    return ""


def _unquote(value: str) -> str:
    """Drop one pair of matching surrounding quotes, as a shell or Compose would."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def require_token(token: str) -> str:
    if not token:
        pytest.fail(
            f"{KEY} is not set in the environment or in .env; the live Crawl4AI tests need it",
            pytrace=False,
        )
    return token
