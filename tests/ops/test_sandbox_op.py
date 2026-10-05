"""`make sandbox` (ADR-0022 check, prod and dev modes) and the dev project name (V1-22, §12)."""

import re
import subprocess
from pathlib import Path

import pytest

from .conftest import ROOT, FakeEnv
from .test_readonly_ops import json_lines

# ---- sandbox ---------------------------------------------------------------------------------

SANDBOX_LOGS = {"FAKE_LOGS_FILE": "crawl4ai_logs.txt"}


def sandbox(fake_env: FakeEnv, tmp_path: Path, mode: str = "prod", **env: str):
    tmp = tmp_path / "tmpdir"
    tmp.mkdir(exist_ok=True)
    if mode == "prod":
        return fake_env.run("sandbox", TMPDIR=str(tmp), **{**SANDBOX_LOGS, **env})
    return fake_env.run(
        "sandbox", mode, script="scripts/check_sandbox.sh", TMPDIR=str(tmp), **SANDBOX_LOGS, **env
    )


def test_sandbox_prod_passes(fake_env: FakeEnv, tmp_path: Path) -> None:
    r = sandbox(fake_env, tmp_path)
    assert r.code == 0, (r.stdout, r.stderr)
    assert len(json_lines(r.stdout)) == 1
    last = r.last
    assert last["ok"] is True and last["command"] == "sandbox" and last["mode"] == "prod"
    assert last["sandbox"] == "on" and last["no_sandbox_procs"] == 0
    assert last["default_ok"] is True and last["builtin_ok"] is True
    assert last["default_new_renderers"] == 2 and last["builtin_new_renderers"] == 2
    assert last["addon_active"] is True and last["addon_warnings"] == 0
    assert last["crawl_ok"] is True and last["builtin_retried"] is False
    assert not any((tmp_path / "tmpdir").iterdir()), "temp crawl output left behind"


def test_sandbox_prod_crawls_inside_container_without_token(
    fake_env: FakeEnv, tmp_path: Path
) -> None:
    r = sandbox(fake_env, tmp_path)
    assert r.code == 0
    calls = fake_env.calls.read_text()
    assert "-p research-engine " in calls and "research-engine-dev" not in calls
    assert "--env-file" in calls
    assert calls.count("exec -T crawl4ai") == 4  # two probes, two crawls
    assert "127.0.0.1:11235/crawl" in calls and "CRAWL4AI_API_TOKEN" in calls  # read in-container
    assert " port " not in calls  # no published ports needed
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr + calls


@pytest.mark.parametrize(
    ("env", "field", "value"),
    [
        ({"FAKE_PROBE_DEFAULT": '{"no_sandbox": 3, "new_renderers": 2, "sandboxed": 2}'},
         "default_ok", False),
        ({"FAKE_PROBE_BUILTIN": '{"no_sandbox": 0, "new_renderers": 2, "sandboxed": 1}'},
         "builtin_ok", False),
        ({"FAKE_PROBE_BUILTIN": '{"no_sandbox": 0, "new_renderers": 0, "sandboxed": 0}'},
         "builtin_ok", False),
        ({"FAKE_PROBE_DEFAULT": "garbage"}, "no_sandbox_procs", -1),
        ({"FAKE_LOGS_FILE": "crawl4ai_logs_noaddon.txt"}, "addon_active", False),
        ({"FAKE_LOGS_FILE": "crawl4ai_logs_warning.txt"}, "addon_warnings", 1),
        ({"FAKE_CRAWL_BUILTIN_FAIL": "1"}, "crawl_ok", False),
    ],
)  # fmt: skip
def test_sandbox_prod_fails(
    fake_env: FakeEnv, tmp_path: Path, env: dict[str, str], field: str, value: object
) -> None:
    r = sandbox(fake_env, tmp_path, **env)
    assert r.code == 1 and len(json_lines(r.stdout)) == 1, (r.stdout, r.stderr)
    assert r.last["ok"] is False and r.last[field] == value


def test_sandbox_builtin_retried_once_on_closed_browser(fake_env: FakeEnv, tmp_path: Path) -> None:
    r = sandbox(fake_env, tmp_path, FAKE_CRAWL_BUILTIN_FAIL_ONCE=str(tmp_path / "failed-once"))
    assert r.code == 0, r.stdout
    assert r.last["ok"] is True and r.last["builtin_retried"] is True


def test_sandbox_never_retries_when_unsandboxed(fake_env: FakeEnv, tmp_path: Path) -> None:
    r = sandbox(
        fake_env,
        tmp_path,
        FAKE_CRAWL_BUILTIN_FAIL_ONCE=str(tmp_path / "failed-once"),
        FAKE_PROBE_BUILTIN='{"no_sandbox": 1, "new_renderers": 2, "sandboxed": 2}',
    )
    assert r.code == 1 and r.last["builtin_retried"] is False and r.last["ok"] is False


def test_sandbox_dev_mode_uses_dev_project(fake_env: FakeEnv, tmp_path: Path) -> None:
    r = sandbox(fake_env, tmp_path, mode="dev")
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.last["ok"] is True and r.last["mode"] == "dev"
    calls = fake_env.calls.read_text()
    compose = [ln for ln in calls.splitlines() if ln.startswith("compose ")]
    dev = r"compose -p research-engine-dev .*-f \S*compose\.yaml -f \S*compose\.dev\.yaml"
    assert compose and all(re.match(dev, c) for c in compose), compose
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr + calls


@pytest.mark.parametrize("args", [(), ("live",)])
def test_sandbox_script_needs_a_mode(fake_env: FakeEnv, args: tuple[str, ...]) -> None:
    r = fake_env.run("sandbox", *args, script="scripts/check_sandbox.sh")
    assert r.code == 2 and len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is False and "usage" in r.last["error"]
    assert not fake_env.calls.exists()


def test_sandbox_docker_failure_one_json_line(fake_env: FakeEnv, tmp_path: Path) -> None:
    r = sandbox(fake_env, tmp_path, FAKE_FAIL="125")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1 and r.last["ok"] is False


# ---- Makefile and the dev project name -------------------------------------------------------


def test_makefile_has_sandbox_target() -> None:
    text = (ROOT / "Makefile").read_text()
    assert re.search(r"^\.PHONY:.*\bsandbox\b", text, re.M)
    assert re.search(r"^sandbox:\s+## .+\n\t@ops/sandbox\.sh$", text, re.M)
    out = subprocess.run(["make", "help"], cwd=ROOT, capture_output=True, text=True, check=True)
    assert re.search(r"^sandbox\s", out.stdout, re.M)


SCAN_SUFFIXES = {".md", ".sh", ".py", ".yaml", ".yml"}
HISTORICAL = ("docs/plans/", "docs/superpowers/")  # plans and specs as written at the time


def _scanned_files() -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [
        f
        for f in tracked
        if (Path(f).suffix in SCAN_SUFFIXES or Path(f).name == "Makefile")
        and not f.startswith(HISTORICAL)
        and (ROOT / f).is_file()
    ]


def _logical_lines(text: str) -> list[str]:
    """Join backslash-continued lines, so a command split over several lines is one line."""
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def dev_invocations_without_project(text: str) -> list[str]:
    return [
        ln
        for ln in _logical_lines(text)
        if "compose.dev.yaml" in ln
        and re.search(r"docker[ -]compose|\bdc\b", ln)
        and "-p research-engine-dev" not in ln
    ]


def test_dev_scan_sees_continued_lines() -> None:
    dev_file = "compose." + "dev.yaml"  # split, so this very file passes the scan
    assert dev_invocations_without_project(f"docker compose \\\n  -f {dev_file} up")
    assert not dev_invocations_without_project(
        "docker compose -p research-engine-dev \\\n  -f compose.yaml -f compose.dev.yaml up"
    )


def test_dev_stack_always_uses_dev_project() -> None:
    """Every dev-stack invocation in a tracked file names the project `research-engine-dev`, so
    it can never replace the live `research-engine` stack."""
    files = _scanned_files()
    assert "CLAUDE.md" in files and "scripts/check_sandbox.sh" in files and "Makefile" in files
    bad = {
        f: hits
        for f in files
        if (hits := dev_invocations_without_project((ROOT / f).read_text(errors="replace")))
    }
    assert not bad, bad


def test_dev_urls_uses_dev_project() -> None:
    text = (ROOT / "scripts" / "dev_urls.sh").read_text()
    assert "docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml" in text
