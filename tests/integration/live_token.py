"""Crawl4AI API token for the live-stack tests: the environment first, then the repo's .env.

Only the one key is parsed (like scripts/check_sandbox.sh); the value is never logged or printed.
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
        if line.startswith(f"{KEY}="):
            return line.split("=", 1)[1].strip()
    return ""


def require_token(token: str) -> str:
    if not token:
        pytest.fail(
            f"{KEY} is not set in the environment or in .env; the live Crawl4AI tests need it",
            pytrace=False,
        )
    return token
