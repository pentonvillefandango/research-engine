"""Fake-bin fixture for ops script tests: a fake `docker` on PATH, canned JSON, argv recorded."""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "docker"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker: records argv, prints canned output chosen by subcommand.
F="$FAKE_FIXTURES"
printf '%s\n' "$*" >> "$FAKE_CALLS"
printf '%s\n' "${GIT_SHA-unset}" >> "$FAKE_CALLS.sha"
args=" $* "
case "$1" in
  compose)
    case "$args" in
      *" ps -q "*) cat "$F/ps_q.txt" ;;
      *" config --services "*) cat "$F/config_services.txt" ;;
      *" images "*) cat "$F/images.json" ;;
      *" logs "*) cat "$F/logs.txt" ;;
      *" exec "*)
        case "$args" in
          *"/health"*) cat "$F/${FAKE_HEALTH_FILE:-exec_health.json}" ;;
          *"/version"*) cat "$F/exec_version.json" ;;
          *) exit 1 ;;
        esac ;;
      *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
    esac ;;
  inspect) cat "$F/${FAKE_INSPECT_FILE:-inspect.json}" ;;
  stats) cat "$F/stats.ndjson" ;;
  system) cat "$F/df.json" ;;
  *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
esac
"""


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    last: dict[str, Any]


@dataclass
class FakeEnv:
    repo: Path
    calls: Path
    secrets: list[str]
    run: Callable[..., Result]

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def fake_env(tmp_path: Path) -> FakeEnv:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(ROOT / "ops", repo / "ops")
    shutil.copy(ROOT / "Makefile", repo / "Makefile")
    secrets = ["SEKRIT-API", "SEKRIT-SX", "SEKRIT-TOK"]
    (repo / ".env").write_text(
        "API_KEY=SEKRIT-API\nSEARXNG_SECRET=SEKRIT-SX\nADMIN_TOKEN=SEKRIT-TOK\n"
        "SITE_HOST=research.example.test\n"
    )
    caddy = tmp_path / "caddy"
    caddy.mkdir()
    (caddy / ".env").write_text("SITE_HOST=research.example.test\n")
    (repo / "backups").mkdir()
    (repo / "backups" / "old.sqlite").write_bytes(b"x" * 10)
    (repo / "deploys.jsonl").write_text(
        '{"ts":"2026-10-04T10:00:00Z","sha":"1111111","result":"ok"}\n'
        '{"ts":"2026-10-05T14:04:54Z","sha":"67374da","result":"ok"}\n'
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    calls = tmp_path / "calls.log"

    def run(cmd: str, *args: str, **env: str) -> Result:
        full = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "FAKE_FIXTURES": str(FIXTURES),
            "FAKE_CALLS": str(calls),
            "GIT_SHA": "abc1234",
            "CADDY_DIR": str(caddy),
            **env,
        }
        p = subprocess.run(
            ["bash", f"ops/{cmd}.sh", *args],
            cwd=repo,
            env=full,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = p.stdout.strip().splitlines()
        try:
            last = json.loads(lines[-1]) if lines else {}
        except ValueError:
            last = {}
        return Result(p.returncode, p.stdout, p.stderr, last)

    return FakeEnv(repo=repo, calls=calls, secrets=secrets, run=run)
