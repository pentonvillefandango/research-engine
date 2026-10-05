import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_policy() -> None:
    df = (ROOT / "Dockerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", df, re.MULTILINE)
    assert froms and all(re.search(r":\d+\.\d+\.\d+", f) for f in froms), froms
    assert not any(f.endswith(":latest") for f in froms)
    assert "ghcr.io/astral-sh/uv:0.12.23" in df
    assert re.search(r"^USER\s+10001", df, re.MULTILINE)
    assert "HEALTHCHECK" in df
    assert "--no-sandbox" not in df
    assert not re.search(r"^ADD\s+https?://", df, re.MULTILINE)
    assert "uv sync --locked" in df


def test_dockerignore() -> None:
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env*" in ignore and "!.env.example" in ignore and ".git" in ignore and "data" in ignore
    assert ".env" in "\n".join(ignore)
