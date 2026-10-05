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

BASH = shutil.which("bash") or "bash"
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "docker"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker: records argv, prints canned output chosen by subcommand.
F="$FAKE_FIXTURES"
printf '%s\n' "$*" >> "$FAKE_CALLS"
printf '%s\n' "${GIT_SHA-unset}" >> "$FAKE_CALLS.sha"
if [ -n "${FAKE_FAIL:-}" ]; then echo "fake docker: simulated failure" >&2; exit "$FAKE_FAIL"; fi
args=" $* "
case "$1" in
  compose)
    case "$args" in
      *" compose version "*) echo "Docker Compose version v0.0.0-fake" ;;
      *" run "*)  # restore: record the mounted source's modes and content, print the CLI result
        src="" prev=""
        for a in "$@"; do [ "$prev" = "-v" ] && src="${a%%:*}"; prev="$a"; done
        printf '%s %s %s\n' "$src" "$(stat -c %a "$src")" "$(stat -c %a "$(dirname "$src")")" \
          >> "$FAKE_CALLS.run"
        cp "$src" "$FAKE_CALLS.run.copy"
        if [ -n "${FAKE_RESTORE_FAIL:-}" ]; then
          echo '{"command": "db restore", "error": "missing tables: event", "ok": false}'; exit 1
        fi
        printf '%s' '{"bytes": 20, "command": "db restore", "from": "/restore.sqlite", "ok": true,'
        echo ' "restored": "/data/research-engine.sqlite"}' ;;
      *" cp app:"*)
        [ -z "${FAKE_CP_FAIL:-}" ] || { echo "fake docker: cp failed" >&2; exit 1; }
        printf 'SQLite format 3 fake backup' > "${*: -1}" ;;
      *" stop app "*) [ -z "${FAKE_STOP_FAIL:-}" ] || exit 1; echo "fake docker: stopped" >&2 ;;
      *" start "*) echo "fake docker: started" >&2 ;;
      *" ps -q caddy "*) [ -n "${FAKE_CADDY_DOWN:-}" ] || echo caddyid ;;
      *" ps -a -q "*) [ -n "${FAKE_PS_EMPTY:-}" ] || cat "$F/ps_q.txt" ;;
      *" ps -q crawl4ai "*) echo c4id ;;
      *" config --services "*)
        [ -z "${FAKE_CONFIG_FAIL:-}" ] || exit 1
        cat "$F/config_services.txt" ;;
      *" images "*) cat "$F/images.json" ;;
      *" logs "*) [ -z "${FAKE_LOGS_FAIL:-}" ] || exit 1; cat "$F/${FAKE_LOGS_FILE:-logs.txt}" ;;
      *" exec "*)
        case "$args" in
          *" research-engine db backup "*)
            if [ -n "${FAKE_DB_BACKUP_FAIL:-}" ]; then
              echo '{"command": "db backup", "error": "database not found", "ok": false}'; exit 1
            fi
            printf '{"bytes": 27, "command": "db backup", "ok": true, "path": "%s"}\n' "${*: -1}" ;;
          *" rm -f /data/.backup-"*) : ;;
          *" caddy reload "*) echo "fake docker: caddy reloaded" >&2 ;;
          *" research-engine smoke "*)
            cat "$F/${FAKE_SMOKE_FILE:-exec_smoke_ok.json}"; exit "${FAKE_SMOKE_EXIT:-0}" ;;
          *"select.select"*)  # sandbox /proc probe: ready, wait for stop, report
            kind="${*: -1}"
            echo ready
            read -r _ || true
            if [ "$kind" = builtin ]; then echo "${FAKE_PROBE_BUILTIN:-$FAKE_PROBE_OK}"
            else echo "${FAKE_PROBE_DEFAULT:-$FAKE_PROBE_OK}"; fi ;;
          *"127.0.0.1:11235/crawl"*)  # in-container crawl
            closed='{"detail":"Target page, context or browser has been closed"}'
            case "$args" in
              *'"browser_mode":"builtin"'*)
                if [ -n "${FAKE_CRAWL_BUILTIN_FAIL:-}" ]; then echo "$closed"
                elif [ -n "${FAKE_CRAWL_BUILTIN_FAIL_ONCE:-}" ] &&
                  [ ! -e "$FAKE_CRAWL_BUILTIN_FAIL_ONCE" ]; then
                  : > "$FAKE_CRAWL_BUILTIN_FAIL_ONCE"; echo "$closed"
                else cat "$F/crawl_ok.json"; fi ;;
              *) cat "$F/crawl_ok.json" ;;
            esac ;;
          *"/health"*) cat "$F/${FAKE_HEALTH_FILE:-exec_health.json}" ;;
          *"/version"*) cat "$F/exec_version.json" ;;
          *) exit 1 ;;
        esac ;;
      *" up -d "*)
        printf '%s\n' "${GIT_SHA-unset}" >> "$FAKE_CALLS.up"
        printf '%s\n' "$PWD" >> "$FAKE_CALLS.up_pwd"
        if [ -n "${FAKE_UP_FAIL_SHA:-}" ] && [ "${GIT_SHA-}" = "$FAKE_UP_FAIL_SHA" ]; then
          echo "fake docker: up failed" >&2; exit 1
        fi
        echo "fake docker: up ok" >&2 ;;
      *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
    esac ;;
  image)
    case "$2" in
      ls) for t in ${FAKE_IMAGE_TAGS:-}; do echo "$t"; done ;;
      rm) [ -z "${FAKE_IMAGE_RM_FAIL:-}" ] || exit 1; echo "Untagged: $3" ;;
      *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
    esac ;;
  ps) for i in ${FAKE_PS_IMAGES:-}; do echo "$i"; done ;;
  inspect)
    case "$args" in
      *"State.StartedAt"*) echo 2026-10-05T10:00:00Z ;;
      *" --format "*) cat "$F/${FAKE_INSPECT_FILE:-inspect.tsv}" ;;
      *) cat "$F/inspect_full.json" ;;
    esac ;;
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
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check_sandbox.sh", repo / "scripts" / "check_sandbox.sh")
    shutil.copy(ROOT / "Makefile", repo / "Makefile")
    secrets = ["SEKRIT-API", "SEKRIT-SX", "SEKRIT-TOK", "SEKRIT-C4"]
    (repo / ".env").write_text(
        "API_KEY=SEKRIT-API\nSEARXNG_SECRET=SEKRIT-SX\nADMIN_TOKEN=SEKRIT-TOK\n"
        "CRAWL4AI_API_TOKEN=SEKRIT-C4\n"
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

    def run(cmd: str, *args: str, script: str | None = None, **env: str) -> Result:
        full = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "FAKE_FIXTURES": str(FIXTURES),
            "FAKE_CALLS": str(calls),
            "GIT_SHA": "abc1234",
            "CADDY_DIR": str(caddy),
            "FAKE_PROBE_OK": '{"no_sandbox": 0, "new_renderers": 2, "sandboxed": 2}',
            **env,
        }
        p = subprocess.run(
            [BASH, script or f"ops/{cmd}.sh", *args],
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
