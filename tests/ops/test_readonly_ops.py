"""Read-only ops commands: status, health, version, logs (V1-22)."""

import re
import subprocess
from pathlib import Path

from .conftest import ROOT, FakeEnv


def test_status_shape_and_no_secrets(fake_env: FakeEnv) -> None:
    r = fake_env.run("status")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["command"] == "status"
    for key in ("containers", "volumes", "version", "last_deploy", "last_backup"):
        assert key in r.last
    app = next(c for c in r.last["containers"] if c["name"] == "research-engine-app-1")
    assert app["state"] == "running" and app["health"] == "healthy"
    assert app["restarts"] == 0 and app["cpu"] == "0.17%" and app["memory"].startswith("336.6MiB")
    assert {v["name"] for v in r.last["volumes"]} == {
        "research-engine_app-data",
        "research-engine_searxng-cache",
    }
    assert r.last["version"]["git_sha"] == "abc1234"
    assert r.last["last_deploy"]["sha"] == "67374da"
    assert r.last["last_backup"]["name"] == "old.sqlite"
    for secret in fake_env.secrets:
        assert secret not in r.stdout and secret not in r.stderr


def test_health_ok(fake_env: FakeEnv) -> None:
    r = fake_env.run("health")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["app_status"] == "up"
    assert r.last["site_host_match"] is True
    assert "research.example.test" not in r.stdout + r.stderr


def test_health_unhealthy_container_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("health", FAKE_INSPECT_FILE="inspect_unhealthy.json")
    assert r.code == 1
    assert r.last["ok"] is False


def test_health_app_down_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("health", FAKE_HEALTH_FILE="exec_health_down.json")
    assert r.code == 1 and r.last["ok"] is False


def test_health_site_host_mismatch_fails_without_printing(fake_env: FakeEnv) -> None:
    caddy_env = fake_env.repo.parent / "caddy" / ".env"
    caddy_env.write_text("SITE_HOST=other.example.test\n")
    r = fake_env.run("health")
    assert r.code == 1
    assert r.last["site_host_match"] is False
    assert "other.example.test" not in r.stdout + r.stderr
    assert "research.example.test" not in r.stdout + r.stderr


def test_version(fake_env: FakeEnv) -> None:
    r = fake_env.run("version")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["git_sha"] == "abc1234"
    assert r.last["version"] == "1.0.0"
    assert any(i["repository"] == "research-engine-app" for i in r.last["images"])


def test_logs_passthrough(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m")
    assert r.code == 0, r.stderr
    assert r.last == {"command": "logs", "lines": 2, "ok": True, "service": "app"}
    assert any("logs --no-color --since 5m app" in c for c in fake_env.docker_calls())


def test_logs_unknown_service_exits_2(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "nope")
    assert r.code == 2 and r.last["ok"] is False


def test_logs_bad_since_exits_2(fake_env: FakeEnv) -> None:
    assert fake_env.run("logs", "app", "yesterday").code == 2


def test_git_sha_defaults_from_deploy_dir(fake_env: FakeEnv) -> None:
    subprocess.run(["git", "init", "-q"], cwd=fake_env.repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        cwd=fake_env.repo,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=fake_env.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    r = fake_env.run("version", GIT_SHA="")
    assert r.code == 0, r.stderr
    seen = set(Path(f"{fake_env.calls}.sha").read_text().split())
    assert seen == {sha}, "dc must export the DEPLOY_DIR HEAD short sha as GIT_SHA"


def test_all_compose_calls_use_project_and_no_destructive(fake_env: FakeEnv) -> None:
    for cmd, args in (("status", ()), ("health", ()), ("version", ()), ("logs", ("app", "1m"))):
        fake_env.run(cmd, *args)
    calls = fake_env.docker_calls()
    assert calls
    for c in calls:
        if c.startswith("compose"):
            assert "-p research-engine " in c, c
        assert "prune" not in c and "down" not in c.split(), c


def test_makefile_targets_and_help() -> None:
    text = (ROOT / "Makefile").read_text()
    phony = re.search(r"^\.PHONY:(.*)$", text, re.M)
    assert phony
    targets = set(phony.group(1).split())
    assert {
        "help", "bootstrap", "deploy", "rollback", "status", "health", "smoke",
        "logs", "backup", "restore", "version", "test",
    } <= targets  # fmt: skip
    out = subprocess.run(["make", "help"], cwd=ROOT, capture_output=True, text=True, check=True)
    for t in targets:
        assert re.search(rf"^{t}\s", out.stdout, re.M), t
