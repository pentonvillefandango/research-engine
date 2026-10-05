"""Documentation checks (V1-22, §8): the docs name what the code really has, and no committed
doc leaks lab values."""

import re
from pathlib import Path

import httpx
import pytest
from research_engine.mcp.server import INSTRUCTIONS, build_mcp

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
USING = ROOT / "docs" / "USING.md"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
DEPLOY_TOOLBOX = ROOT / "docs" / "deploy-toolbox.md"
ADR_DIR = ROOT / "docs" / "adr"
USING_MAX_WORDS = 1800
EXAMPLE_HOST = "research.toolbox.home.arpa"

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}  # noqa: S104 - a text allow-list, not a bind address
HOME_PATH = re.compile(r"/home/[\w.-]+")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _scanned_docs() -> list[Path]:
    """Committed docs that must hold no lab values. Plans and specs are excluded: their test
    code deliberately uses example public and private addresses."""
    names = ["README.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md"]
    files = [ROOT / n for n in names]
    files += [OPERATIONS, DEPLOY_TOOLBOX, USING]
    files += sorted(ADR_DIR.glob("*.md"))
    return files


def _make_targets() -> list[str]:
    text = _read(ROOT / "Makefile")
    return re.findall(r"^([a-z][a-z0-9-]*):", text, flags=re.MULTILINE)


def _env_example_keys() -> list[str]:
    text = _read(ROOT / ".env.example")
    return re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)


# ---- README ------------------------------------------------------------------------------


def test_readme_links_using_guide_near_the_top() -> None:
    lines = _read(README).splitlines()
    first = "\n".join(lines[:30])
    assert "(docs/USING.md)" in first


def test_readme_config_table_lists_every_env_example_key() -> None:
    table_lines = [ln for ln in _read(README).splitlines() if ln.lstrip().startswith("|")]
    table = "\n".join(table_lines)
    keys = _env_example_keys()
    assert len(keys) > 20  # the parse saw the file
    missing = [k for k in keys if f"`{k}`" not in table]
    assert not missing, missing


def test_readme_names_example_host_only_in_deployment_section() -> None:
    section = ""
    bad: list[str] = []
    for ln in _read(README).splitlines():
        if ln.startswith("#"):
            section = ln
        if EXAMPLE_HOST in ln and "deploy" not in section.lower():
            bad.append(f"{section}: {ln}")
    assert not bad, bad


# ---- OPERATIONS --------------------------------------------------------------------------


def test_operations_mentions_every_make_target() -> None:
    text = _read(OPERATIONS)
    targets = _make_targets()
    assert {"deploy", "rollback", "backup", "restore", "bootstrap"} <= set(targets)
    missing = [t for t in targets if f"make {t}" not in text]
    assert not missing, missing


# ---- lab values and ADR shape ------------------------------------------------------------


def test_scan_helpers_catch_lab_values() -> None:
    assert IPV4.findall("host 10.1.2.3 here") == ["10.1.2.3"]
    assert IPV4.findall("crawl4ai 0.9.4 and v1.0.0") == []
    assert HOME_PATH.search("see /home/someone/x")
    assert not HOME_PATH.search("no `/home/` paths")


def test_no_lab_ips_or_home_paths_in_docs() -> None:
    problems: list[str] = []
    for path in _scanned_docs():
        text = _read(path)
        rel = path.relative_to(ROOT)
        problems += [f"{rel}: {ip}" for ip in IPV4.findall(text) if ip not in ALLOWED_IPS]
        problems += [f"{rel}: {m}" for m in HOME_PATH.findall(text)]
    assert not problems, problems


def test_every_adr_has_the_required_sections() -> None:
    adrs = sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert len(adrs) >= 26
    problems = [
        f"{p.name}: {s}"
        for p in adrs
        for s in ("Status", "Context", "Decision", "Consequences")
        if not re.search(rf"^## {s}\s*$", _read(p), flags=re.MULTILINE)
    ]
    assert not problems, problems


# ---- USING.md (the agent usage guide) ----------------------------------------------------


def test_using_guide_exists_and_is_short() -> None:
    words = len(_read(USING).split())
    assert 0 < words <= USING_MAX_WORDS, words


async def test_using_guide_names_every_mcp_tool() -> None:
    tools = [t.name for t in await build_mcp(lambda: pytest.fail("not called")).list_tools()]
    assert len(tools) == 4
    text = _read(USING)
    missing = [t for t in tools if f"`{t}`" not in text]
    assert not missing, missing


async def test_using_guide_names_every_v1_route(client: httpx.AsyncClient) -> None:
    """Method and path, e.g. ``POST /v1/search`` and ``DELETE /v1/jobs/{job_id}``."""
    spec = (await client.get("/openapi.json")).json()
    routes = [
        f"{method.upper()} {path}"
        for path, ops in spec["paths"].items()
        if path.startswith("/v1")
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert "POST /v1/search" in routes and "DELETE /v1/jobs/{job_id}" in routes
    text = _read(USING)
    # whole route only: "POST /v1/search" must not be satisfied by "POST /v1/search_read"
    missing = [r for r in routes if not re.search(re.escape(r) + r"(?![\w/{])", text)]
    assert not missing, missing


def test_using_guide_covers_errors_and_untrusted_content() -> None:
    text = _read(USING)
    assert "retryable" in text
    assert "untrusted" in text


async def test_mcp_instructions_consistent_with_guide() -> None:
    """The server's own instructions name every tool and the untrusted-content rule, as the
    guide does."""
    tools = [t.name for t in await build_mcp(lambda: pytest.fail("not called")).list_tools()]
    missing = [t for t in tools if t not in INSTRUCTIONS]
    assert not missing, missing
    assert "untrusted" in INSTRUCTIONS
