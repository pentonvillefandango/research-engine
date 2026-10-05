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


@pytest.mark.parametrize(
    "line",
    [
        'CRAWL4AI_API_TOKEN="tok-en"',
        "CRAWL4AI_API_TOKEN='tok-en'",
        "export CRAWL4AI_API_TOKEN=tok-en",
        'export CRAWL4AI_API_TOKEN="tok-en"',
        "CRAWL4AI_API_TOKEN=tok-en\r",
        'CRAWL4AI_API_TOKEN="tok-en"\r',
        "export\tCRAWL4AI_API_TOKEN=tok-en",
        "CRAWL4AI_API_TOKEN=tok-en # inline comment",
        "CRAWL4AI_API_TOKEN=tok-en\t# inline comment",
        'CRAWL4AI_API_TOKEN="tok-en" # inline comment',
    ],
)
def test_env_file_value_is_normalised(tmp_path: Path, line: str) -> None:
    """Surrounding quotes, a leading ``export `` and a CRLF ``\\r`` are not part of the token."""
    env_file = tmp_path / ".env"
    env_file.write_bytes(f"OTHER=x\n{line}\nNEXT=y\n".encode())
    assert crawl4ai_token({}, env_file) == "tok-en"


def test_hash_inside_value_is_kept(tmp_path: Path) -> None:
    """Only whitespace-then-# starts a comment (as in Compose); a quoted # is data."""
    env_file = tmp_path / ".env"
    env_file.write_text("CRAWL4AI_API_TOKEN=tok#en\n")
    assert crawl4ai_token({}, env_file) == "tok#en"
    env_file.write_text('CRAWL4AI_API_TOKEN="tok # en" # comment\n')
    assert crawl4ai_token({}, env_file) == "tok # en"
