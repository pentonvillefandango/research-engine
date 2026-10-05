"""Read-only ops commands: status, health, version, logs (V1-22)."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import ROOT, FakeEnv

BASH = shutil.which("bash") or "bash"


def json_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith("{") and ln.endswith("}")]


def lib(
    fake_env: FakeEnv, snippet: str, stdin: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, "GIT_SHA": "abc1234", "REPO_DIR": str(fake_env.repo), **env}
    return subprocess.run(
        [BASH, "-c", f". {fake_env.repo}/ops/lib.sh; {snippet}"],
        input=stdin,
        env=full,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---- status ----------------------------------------------------------------------------------


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


def test_inspect_never_requests_config_env(fake_env: FakeEnv) -> None:
    """Every `docker inspect` uses --format (so Config.Env is never read); no secret leaks."""
    outs = [fake_env.run(c) for c in ("status", "health", "version")]
    inspects = [c for c in fake_env.docker_calls() if c.startswith("inspect")]
    assert inspects and all(" --format " in f" {c} " for c in inspects)
    for r in outs:
        for secret in fake_env.secrets:
            assert secret not in r.stdout + r.stderr


def test_status_stopped_stack_reports_state(fake_env: FakeEnv) -> None:
    r = fake_env.run("status", FAKE_PS_EMPTY="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is False and r.last["containers"] == []
    assert sorted(r.last["not_running"]) == ["app", "crawl4ai", "searxng"]
    assert "volumes" in r.last and r.last["last_deploy"]["sha"] == "67374da"


def test_status_flags_exited_container(fake_env: FakeEnv) -> None:
    r = fake_env.run("status", FAKE_INSPECT_FILE="inspect_stopped.tsv")
    assert r.code == 1 and r.last["not_running"] == ["crawl4ai"]
    assert any(c["state"] == "exited" for c in r.last["containers"])


def test_status_skips_blank_deploy_lines(fake_env: FakeEnv) -> None:
    log = fake_env.repo / "deploys.jsonl"
    log.write_text(log.read_text() + "\n\n  \n")
    r = fake_env.run("status")
    assert r.last["last_deploy"]["sha"] == "67374da"


# ---- health ----------------------------------------------------------------------------------


def test_health_ok(fake_env: FakeEnv) -> None:
    r = fake_env.run("health")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["app_status"] == "up" and r.last["problems"] == []
    assert r.last["site_host_match"] is True
    assert "research.example.test" not in r.stdout + r.stderr
    assert any("ps -a -q" in c for c in fake_env.docker_calls())


@pytest.mark.parametrize(
    "name", ["SEARXNG_SECRET", "API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN"]
)
@pytest.mark.parametrize("bad", ["change-me", "short-but-unique-value", None])
def test_health_flags_placeholder_short_or_missing_secret(
    fake_env: FakeEnv, name: str, bad: str | None
) -> None:
    env = fake_env.repo / ".env"
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith(f"{name}=")]
    if bad is not None:
        lines.append(f"{name}={bad}")
    env.write_text("\n".join(lines) + "\n")
    r = fake_env.run("health")
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["weak_secrets"] == [name]
    assert any(name in p and "openssl rand -hex 32" in p for p in r.last["problems"])
    if bad is not None:
        assert bad not in r.stdout + r.stderr


def test_health_unhealthy_container_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("health", FAKE_INSPECT_FILE="inspect_unhealthy.tsv")
    assert r.code == 1 and r.last["ok"] is False
    assert sorted(p.split(":")[0] for p in r.last["problems"]) == ["crawl4ai", "searxng"]


def test_health_stopped_service_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("health", FAKE_INSPECT_FILE="inspect_stopped.tsv")
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["problems"] == ["crawl4ai: not running and healthy"]


def test_health_missing_service_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("health", FAKE_INSPECT_FILE="inspect_missing.tsv")
    assert r.code == 1 and r.last["problems"] == ["crawl4ai: not running and healthy"]


def test_health_app_down_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("health", FAKE_HEALTH_FILE="exec_health_down.json")
    assert r.code == 1 and r.last["ok"] is False and r.last["app_status"] == "down"


def test_app_get_keeps_http_error_body() -> None:
    src = (ROOT / "ops" / "lib.sh").read_text()
    assert "urllib.error.HTTPError" in src and "r = e" in src


@pytest.mark.parametrize(
    ("caddy_line", "match", "ok"),
    [
        ('export SITE_HOST="Research.Example.Test"  # shared\r', True, True),
        ("SITE_HOST='research.example.test'", True, True),
        ("SITE_HOST=other.example.test", False, False),
        ("OTHER=1", False, False),
    ],
)
def test_health_site_host_variants(
    fake_env: FakeEnv, caddy_line: str, match: bool, ok: bool
) -> None:
    (fake_env.repo.parent / "caddy" / ".env").write_text(caddy_line + "\n")
    r = fake_env.run("health")
    assert r.last["site_host_match"] is match and (r.code == 0) is ok
    out = (r.stdout + r.stderr).lower()
    assert "example.test" not in out


def test_health_no_caddy_env_is_null_not_failure(fake_env: FakeEnv) -> None:
    (fake_env.repo.parent / "caddy" / ".env").unlink()
    r = fake_env.run("health")
    assert r.code == 0 and r.last["site_host_match"] is None
    assert r.last["site_host_reason"] == "no caddy env"


def test_health_unreadable_caddy_env_fails(fake_env: FakeEnv) -> None:
    path = fake_env.repo.parent / "caddy" / ".env"
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        r = fake_env.run("health")
        assert r.code == 1 and r.last["site_host_reason"] == "unreadable caddy env"
    finally:
        path.chmod(0o600)


def test_health_mismatch_reason(fake_env: FakeEnv) -> None:
    (fake_env.repo.parent / "caddy" / ".env").write_text("SITE_HOST=other.example.test\n")
    r = fake_env.run("health")
    assert r.code == 1 and r.last["site_host_reason"] == "mismatch"


# ---- version ---------------------------------------------------------------------------------


def test_version(fake_env: FakeEnv) -> None:
    r = fake_env.run("version")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["git_sha"] == "abc1234" and r.last["version"] == "1.0.0"
    assert any(i["repository"] == "research-engine-app" for i in r.last["images"])


# ---- logs ------------------------------------------------------------------------------------


def test_logs_passthrough(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m")
    assert r.code == 0, r.stderr
    assert r.last == {"command": "logs", "lines": 2, "ok": True, "service": "app"}
    assert any("logs --no-color --since 5m --tail 500 app" in c for c in fake_env.docker_calls())


def test_logs_tail_env(fake_env: FakeEnv) -> None:
    fake_env.run("logs", "app", "5m", TAIL="7")
    assert any("--tail 7 app" in c for c in fake_env.docker_calls())
    assert fake_env.run("logs", "app", "5m", TAIL="x").code == 2


def test_logs_redacts_secrets(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m", FAKE_LOGS_FILE="logs_secrets.txt")
    assert r.code == 0
    assert "SEKRIT" not in r.stdout + r.stderr
    assert "visible-message" in r.stdout


def test_logs_unknown_service_exits_2(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "nope")
    assert r.code == 2 and r.last["ok"] is False


@pytest.mark.parametrize("svc", [".*", "ap.", "a b", 'a"; echo INJECTED; "', "$(id)"])
def test_logs_service_is_literal(fake_env: FakeEnv, svc: str) -> None:
    r = fake_env.run("logs", svc)
    assert r.code == 2 and r.last["error"] == "unknown service"
    assert r.last["service"] == svc and len(json_lines(r.stdout)) == 1


def test_logs_bad_since_exits_2(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "null")
    assert r.code == 2 and r.last["service"] == "app"
    assert isinstance(r.last["error"], str)


def test_logs_docker_failure_is_ok_false(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m", FAKE_LOGS_FAIL="1")
    assert r.code == 1 and r.last["ok"] is False
    assert r.stdout.strip().splitlines()[-1].startswith('{"command": "logs"')


def test_logs_config_failure_is_exit_1(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m", FAKE_CONFIG_FAIL="1")
    assert r.code == 1 and r.last["ok"] is False


# ---- lib / failure contract ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["status", "health", "version", "logs"])
def test_docker_failure_still_one_json_line(fake_env: FakeEnv, cmd: str) -> None:
    r = fake_env.run(cmd, FAKE_FAIL="125")
    assert r.code == 1
    assert len(json_lines(r.stdout)) == 1 and r.last["ok"] is False and r.last["command"] == cmd
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr


def test_success_emits_exactly_one_json_line(fake_env: FakeEnv) -> None:
    for cmd in ("status", "health", "version"):
        assert len(json_lines(fake_env.run(cmd).stdout)) == 1, cmd


def _restricted_path(tmp: Path, *tools: str) -> str:
    d = tmp / "only"
    d.mkdir(exist_ok=True)
    for t in tools:
        src = shutil.which(t)
        assert src
        (d / t).symlink_to(src)
    return str(d)


def test_missing_docker_exits_2(fake_env: FakeEnv) -> None:
    path = _restricted_path(fake_env.repo.parent, "dirname", "python3")
    r = fake_env.run("status", PATH=path)
    assert r.code == 2 and r.last["ok"] is False and "docker" in r.last["error"]


def test_missing_python3_exits_2_with_json(fake_env: FakeEnv) -> None:
    path = _restricted_path(fake_env.repo.parent, "dirname")
    r = fake_env.run("status", PATH=path)
    assert r.code == 2 and r.last["ok"] is False and "python3" in r.last["error"]


def test_undeterminable_git_sha_exits_2(fake_env: FakeEnv) -> None:
    r = fake_env.run("version", GIT_SHA="")
    assert r.code == 2 and "GIT_SHA" in r.last["error"]


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
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=fake_env.repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    r = fake_env.run("version", GIT_SHA="")
    assert r.code == 0, r.stderr
    seen = set(Path(f"{fake_env.calls}.sha").read_text().split())
    assert seen == {sha}, "dc must export the DEPLOY_DIR HEAD short sha as GIT_SHA"
    assert len(sha) == 12, "fixed short length, so a sha's image tag never changes"


def test_app_port_adds_debug_compose_file(fake_env: FakeEnv) -> None:
    fake_env.run("version")
    assert not any("compose.debug.yaml" in c for c in fake_env.docker_calls())
    (fake_env.repo / ".env").write_text((fake_env.repo / ".env").read_text() + "APP_PORT=8123\n")
    fake_env.run("version")
    assert any("compose.debug.yaml" in c for c in fake_env.docker_calls())


def test_json_out_keeps_strings_unless_typed(fake_env: FakeEnv) -> None:
    p = lib(fake_env, 'json_out a=null b=123 c:=123 d:=true e="x=y"')
    assert json.loads(p.stdout) == {"a": "null", "b": "123", "c": 123, "d": True, "e": "x=y"}


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("X=plain", "plain"),
        ("export X=exported", "exported"),
        ('X="quoted value"', "quoted value"),
        ("X='single'", "single"),
        ("X=val # comment", "val"),
        ("X=a#b", "a#b"),
        ("X=a=b=c", "a=b=c"),
        ("X=crlf\r", "crlf"),
        ('X="q # kept" # dropped', "q # kept"),
    ],
)
def test_env_get_normalises(fake_env: FakeEnv, line: str, want: str) -> None:
    (fake_env.repo / "t.env").write_text(f"OTHER=1\n{line}\n")
    p = lib(fake_env, f'env_get X "{fake_env.repo}/t.env"')
    assert p.stdout.rstrip("\n") == want


def test_env_get_missing(fake_env: FakeEnv) -> None:
    assert lib(fake_env, "env_get NOPE").returncode == 1


@pytest.mark.parametrize(
    "line",
    [
        "API_KEY=SEKRIT-1 tail",
        "api_key=SEKRIT-1",
        'password="SEKRIT-1 with spaces"',
        "my_Token = SEKRIT-1",
        '{"api_key": "SEKRIT-1", "x": 1}',
        '{"Auth_Token":"SEKRIT-1"}',
        "X-API-Key: SEKRIT-1",
        "authorization: Bearer SEKRIT-1",
        "Authorization: Bearer SEKRIT-1 extra",
        "SEARXNG_SECRET='SEKRIT-1 two'",
    ],
)
def test_redact_masks(fake_env: FakeEnv, line: str) -> None:
    p = lib(fake_env, "redact", stdin=line + "\n")
    assert "SEKRIT" not in p.stdout and "***REDACTED***" in p.stdout, p.stdout
    assert "with spaces" not in p.stdout and "two" not in p.stdout.split("REDACTED")[-1]


@pytest.mark.parametrize(
    "line",
    [
        # unquoted values continue after a space, up to the end of the line
        "API_KEY=SEKRIT-1 SEKRIT-2",
        "PASSWORD=SEKRIT-1 SEKRIT-2 and more",
        "my_token = SEKRIT-1 SEKRIT-2",
        # colon form, case-insensitive
        "password: SEKRIT-1",
        "Password: SEKRIT-1 SEKRIT-2",
        "api_key: SEKRIT-1",
        "API_KEY : SEKRIT-1",
        "db_secret: 'SEKRIT-1 SEKRIT-2'",
        'searxng_secret: "SEKRIT-1 SEKRIT-2"',
        "  crawl4ai_api_token:SEKRIT-1",
        # cookie headers
        "Cookie: re_session=SEKRIT-1; other=SEKRIT-2",
        "cookie: SEKRIT-1",
        "Set-Cookie: re_session=SEKRIT-1; Path=/; HttpOnly; SameSite=Lax",
        "< set-cookie: re_session=SEKRIT-1",
    ],
)
def test_redact_masks_spaced_colon_and_cookie_forms(fake_env: FakeEnv, line: str) -> None:
    p = lib(fake_env, "redact", stdin=line + "\n")
    assert "SEKRIT" not in p.stdout and "***REDACTED***" in p.stdout, p.stdout


def test_redact_colon_form_keeps_the_name(fake_env: FakeEnv) -> None:
    p = lib(fake_env, "redact", stdin="Set-Cookie: sid=SEKRIT-1\npassword: SEKRIT-2\n")
    assert p.stdout == "Set-Cookie: ***REDACTED***\npassword: ***REDACTED***\n"


@pytest.mark.parametrize(
    "line",
    ["a" * 20_000, "key" * 1000, "key=" + " " * 20_000, "x_token: " + "ab " * 7000],
    ids=["20k-plain", "key-x1000", "key=-20k-spaces", "20k-value"],
)
def test_redact_is_fast_on_pathological_lines(fake_env: FakeEnv, line: str) -> None:
    """No catastrophic backtracking: a long base64 blob or JWT must not stall logs/deploy."""
    t0 = time.monotonic()
    p = lib(fake_env, "redact", stdin=line + "\n")
    assert time.monotonic() - t0 < 1.0
    assert p.returncode == 0 and p.stdout.endswith("\n")


def test_redact_keeps_harmless_lines(fake_env: FakeEnv) -> None:
    msg = '{"level":"info","msg":"fetched https://example.test/a?b=c","status":200}\n'
    assert lib(fake_env, "redact", stdin=msg).stdout == msg
    plain = "Content-Type: text/html\nstatus: 200\nGET https://example.test/ 200 OK\n"
    assert lib(fake_env, "redact", stdin=plain).stdout == plain


# ---- Makefile --------------------------------------------------------------------------------


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


@pytest.mark.parametrize(
    ("target", "var"),
    [("logs", "SERVICE"), ("logs", "SINCE"), ("restore", "FILE")],
)
def test_makefile_vars_are_not_injectable(target: str, var: str) -> None:
    evil = 'a"; echo INJECTED; "$(id)`id`'
    out = subprocess.run(
        ["make", "-n", target, f"{var}={evil}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "INJECTED" not in out and "uid=" not in out
    assert f'"${var}"' in out
