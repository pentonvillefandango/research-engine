"""The integration tests' Crawl4AI token lookup: environment first, then .env; never printed."""

from pathlib import Path

import pytest

from tests.integration.live_token import crawl4ai_token, require_token


def test_environment_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CRAWL4AI_API_TOKEN=from-file\n")
    assert crawl4ai_token({"CRAWL4AI_API_TOKEN": "from-env"}, env_file) == "from-env"


def test_falls_back_to_env_file_key_only(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# CRAWL4AI_API_TOKEN=commented\nOTHER=x\n"
        "CRAWL4AI_API_TOKEN=tok=en\nX_CRAWL4AI_API_TOKEN=no\n"
    )
    assert crawl4ai_token({}, env_file) == "tok=en"
    assert crawl4ai_token({"CRAWL4AI_API_TOKEN": ""}, env_file) == "tok=en"


def test_missing_file_gives_empty(tmp_path: Path) -> None:
    assert crawl4ai_token({}, tmp_path / "absent") == ""


def test_require_token_fails_fast_without_leaking() -> None:
    with pytest.raises(pytest.fail.Exception, match="CRAWL4AI_API_TOKEN is not set"):
        require_token("")
    assert require_token("secret-value") == "secret-value"
