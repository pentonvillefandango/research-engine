import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _directives() -> list[str]:
    """Dockerfile lines with comments dropped and continuations joined."""
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _final_stage(lines: list[str]) -> list[str]:
    last = max(i for i, ln in enumerate(lines) if re.match(r"FROM\s", ln))
    return lines[last:]


def test_base_images_pinned() -> None:
    froms = [ln.split()[1] for ln in _directives() if re.match(r"FROM\s", ln)]
    assert len(froms) >= 2
    assert all(re.search(r":\d+\.\d+\.\d+", f) and not f.endswith(":latest") for f in froms), froms


def test_uv_pinned_on_copy_from() -> None:
    assert any(
        ln.startswith("COPY") and "--from=ghcr.io/astral-sh/uv:0.12.23 " in ln
        for ln in _directives()
    )


def test_final_stage_non_root_with_healthcheck() -> None:
    final = _final_stage(_directives())
    assert any(re.fullmatch(r"USER\s+10001", ln) for ln in final)
    assert not any(re.match(r"USER\s+(root|0)\b", ln) for ln in final)
    hc = [ln for ln in final if ln.startswith("HEALTHCHECK")]
    assert hc and not any(re.match(r"HEALTHCHECK\s+NONE", ln) for ln in hc)


def test_no_unsafe_directives() -> None:
    lines = _directives()
    assert not any("--no-sandbox" in ln for ln in lines)
    assert not any(re.match(r"ADD\s+https?://", ln) for ln in lines)


def test_both_uv_syncs_locked() -> None:
    syncs = [ln for ln in _directives() if "uv sync" in ln]
    assert len(syncs) >= 2
    assert all("uv sync --locked" in ln for ln in syncs), syncs


def test_app_code_root_owned() -> None:
    final = _final_stage(_directives())
    assert not any(ln.startswith("COPY") and "--chown" in ln for ln in final)


def test_dockerignore() -> None:
    ignore = [
        ln.strip()
        for ln in (ROOT / ".dockerignore").read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    env = [i for i, ln in enumerate(ignore) if ln.endswith(".env*")]
    assert env and "!.env.example" in ignore
    assert ignore.index("!.env.example") > max(env)
    for entry in (".git", "data", "backups", "logs", "deploys.jsonl", ".venv", "tests", "docs"):
        assert entry in ignore, entry
