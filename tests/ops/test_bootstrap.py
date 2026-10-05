"""`make bootstrap` (V1-22, §9, §10): idempotent VM setup; fake docker, sudo and systemctl."""

import hashlib
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .conftest import ROOT, FakeEnv, Result
from .test_readonly_ops import json_lines

SECRET_NAMES = ["API_KEY", "SESSION_SECRET", "SEARXNG_SECRET", "CRAWL4AI_API_TOKEN"]
UNITS = ["research-engine-backup.service", "research-engine-backup.timer"]
LAB = "10.99.0.0/24 10.98.0.0/24"
TOOLBOX = "toolbox.lab.example.test"

FAKE_SUDO = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CALLS.sudo"
exec "$@"
"""
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CALLS.systemctl"
case "$1" in
  is-enabled|is-active) [ -e "$FAKE_TIMER_STATE" ] ;;
  enable) : > "$FAKE_TIMER_STATE" ;;
  *) : ;;
esac
"""


@dataclass
class Boot:
    env: FakeEnv
    repo: Path
    caddy: Path
    systemd: Path
    root: Path
    extra: dict[str, str]

    def run(self, *args: str, **env: str) -> Result:
        r = self.env.run("bootstrap", *args, **{**self.extra, **env})
        assert len(json_lines(r.stdout)) == 1, (r.stdout, r.stderr)
        assert r.stdout.strip().splitlines()[-1] == json_lines(r.stdout)[0]
        return r

    def calls(self, kind: str) -> list[str]:
        p = Path(f"{self.env.calls}.{kind}") if kind else self.env.calls
        return p.read_text().splitlines() if p.exists() else []

    def sync_caddy(self) -> None:
        """Make $CADDY_DIR look installed and current (as on a bootstrapped VM)."""
        shutil.copytree(self.repo / "deploy" / "caddy", self.caddy, dirs_exist_ok=True)


@pytest.fixture
def boot(fake_env: FakeEnv, tmp_path: Path) -> Boot:
    repo = fake_env.repo
    shutil.copy(ROOT / ".env.example", repo / ".env.example")
    shutil.copytree(ROOT / "deploy" / "caddy", repo / "deploy" / "caddy")
    shutil.copytree(ROOT / "deploy" / "systemd", repo / "deploy" / "systemd")
    (repo / ".env").unlink()  # a fresh VM; tests that need one write it
    shutil.rmtree(repo / "backups")
    caddy = tmp_path / "caddy"
    (caddy / ".env").unlink()
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    bindir = tmp_path / "bin"
    for name, body in (("sudo", FAKE_SUDO), ("systemctl", FAKE_SYSTEMCTL)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    extra = {"SYSTEMD_DIR": str(systemd), "FAKE_TIMER_STATE": str(tmp_path / "timer-on")}
    return Boot(fake_env, repo, caddy, systemd, tmp_path, extra)


def snapshot(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in sorted(root.rglob("*")):
        if p.name.startswith("calls.log"):
            continue
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        out[str(p.relative_to(root))] = (stat.S_IMODE(st.st_mode), st.st_mtime_ns, digest)
    return out


def actions(r: Result) -> dict[str, dict[str, Any]]:
    return {a["step"]: a for a in r.last["actions"]}


def env_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if re.match(r"^[A-Z][A-Z0-9_]*=", line)
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---- dry run ---------------------------------------------------------------------------------


def test_dry_run_on_a_fresh_vm_plans_everything_and_changes_nothing(boot: Boot) -> None:
    before = snapshot(boot.root)
    r = boot.run("--dry-run", LAB_SUBNET=LAB)
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["command"] == "bootstrap" and r.last["dry_run"] is True
    a = actions(r)
    assert list(a) == ["env", "backup_dir", "caddy_files", "caddy_env", "caddy", "systemd"]
    assert a["env"]["action"] == "create" and a["backup_dir"]["action"] == "create"
    assert a["caddy_files"]["action"] == "update"
    assert {"Caddyfile", "compose.yaml", "sites/research-engine.caddy"} <= set(
        a["caddy_files"]["changed"]
    )
    assert ".env" not in a["caddy_files"]["changed"]
    assert a["caddy_env"] == {
        "step": "caddy_env",
        "action": "create",
        "keys": ["SITE_HOST", "LAB_SUBNET"],
    }
    assert a["caddy"]["action"] == "up" and a["caddy"]["running"] is False
    assert a["systemd"]["action"] == "install" and a["systemd"]["units"] == UNITS
    assert snapshot(boot.root) == before
    assert boot.calls("sudo") == []
    assert not [c for c in boot.calls("systemctl") if not c.startswith(("is-enabled", "is-active"))]
    assert all(re.search(r" (version|ps -q caddy)$", f" {c}") for c in boot.calls(""))


def test_dry_run_on_a_bootstrapped_vm_plans_no_changes(boot: Boot) -> None:
    assert boot.run(LAB_SUBNET=LAB).code == 0
    before = snapshot(boot.root)
    r = boot.run("--dry-run")
    assert r.code == 0, r.stderr
    a = actions(r)
    assert [a[s]["action"] for s in a] == ["keep", "keep", "keep", "keep", "up", "keep"]
    assert a["caddy"]["running"] is True
    assert snapshot(boot.root) == before


def test_missing_caddy_env_without_lab_subnet_exits_2_changing_nothing(boot: Boot) -> None:
    before = snapshot(boot.root)
    for args in ((), ("--dry-run",)):
        r = boot.run(*args)
        assert r.code == 2 and r.last["ok"] is False and "LAB_SUBNET" in r.last["error"]
    assert snapshot(boot.root) == before
    assert boot.calls("sudo") == []


def test_unknown_argument_is_a_usage_error(boot: Boot) -> None:
    r = boot.run("--force")
    assert r.code == 2 and "usage" in r.last["error"]


def test_docker_compose_unavailable_exits_2(boot: Boot) -> None:
    r = boot.run("--dry-run", LAB_SUBNET=LAB, FAKE_FAIL="1")
    assert r.code == 2 and "docker compose" in r.last["error"]


# ---- a real run ------------------------------------------------------------------------------


def test_fresh_run_creates_env_with_private_distinct_secrets(boot: Boot) -> None:
    r = boot.run(LAB_SUBNET=LAB, TOOLBOX_HOST=TOOLBOX)
    assert r.code == 0, r.stderr
    env = boot.repo / ".env"
    assert mode(env) == 0o600
    values = env_values(env)
    secrets = [values[n] for n in SECRET_NAMES]
    assert all(re.fullmatch(r"[0-9a-f]{64}", s) for s in secrets)
    assert len(set(secrets)) == 4
    for s in secrets:
        assert s not in r.stdout + r.stderr
    # every other line of the example is kept as is
    example = env_values(boot.repo / ".env.example")
    assert {k: v for k, v in values.items() if k not in SECRET_NAMES} == {
        k: v for k, v in example.items() if k not in SECRET_NAMES
    }
    assert not list(boot.repo.glob(".env.bootstrap-tmp"))


def test_fresh_run_installs_caddy_backup_dir_and_timer(boot: Boot) -> None:
    r = boot.run(LAB_SUBNET=LAB, TOOLBOX_HOST=TOOLBOX)
    assert r.code == 0, r.stderr
    assert r.last["dry_run"] is False
    assert mode(boot.repo / "backups") == 0o700
    for rel in ("Caddyfile", "compose.yaml", "sites/research-engine.caddy", "index/index.html"):
        assert (boot.caddy / rel).read_bytes() == (boot.repo / "deploy/caddy" / rel).read_bytes()
    cenv = boot.caddy / ".env"
    assert mode(cenv) == 0o600
    assert env_values(cenv) == {
        "SITE_HOST": env_values(boot.repo / ".env")["SITE_HOST"],
        "LAB_SUBNET": LAB,
        "TOOLBOX_HOST": TOOLBOX,
    }
    assert LAB not in r.stdout + r.stderr and TOOLBOX not in r.stdout + r.stderr
    ups = [c for c in boot.calls("") if " up -d " in f" {c} "]
    assert ups == ["compose up -d --wait"]
    assert Path(f"{boot.env.calls}.up_pwd").read_text().split() == [str(boot.caddy)]
    sudo = boot.calls("sudo")
    assert [c.split()[:3] for c in sudo[:2]] == [["install", "-m", "644"]] * 2
    assert [c.split()[-1] for c in sudo[:2]] == [str(boot.systemd / u) for u in UNITS]
    assert sudo[2:] == [
        "systemctl daemon-reload",
        "systemctl enable --now research-engine-backup.timer",
    ]
    svc = (boot.systemd / UNITS[0]).read_text()
    assert "@REPO_DIR@" not in svc and "@USER@" not in svc
    assert f"WorkingDirectory={boot.repo}" in svc
    assert f"ExecStart=/usr/bin/make -C {boot.repo} backup" in svc
    assert re.search(r"^User=\S+$", svc, re.M) and "Type=oneshot" in svc
    timer = (boot.systemd / UNITS[1]).read_text()
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer
    assert mode(boot.systemd / UNITS[0]) == 0o644


def test_second_run_is_a_no_op_without_sudo(boot: Boot) -> None:
    assert boot.run(LAB_SUBNET=LAB).code == 0
    env_before = (boot.repo / ".env").read_bytes()
    before = snapshot(boot.root)
    sudo_before = len(boot.calls("sudo"))
    r = boot.run()  # LAB_SUBNET no longer needed: the Caddy env exists
    assert r.code == 0, r.stderr
    a = actions(r)
    assert [a[s]["action"] for s in a] == ["keep", "keep", "keep", "keep", "up", "keep"]
    assert len(boot.calls("sudo")) == sudo_before
    assert (boot.repo / ".env").read_bytes() == env_before
    assert snapshot(boot.root) == before
    assert not any("force-recreate" in c or "reload" in c for c in boot.calls(""))


def test_existing_env_is_never_overwritten(boot: Boot) -> None:
    env = boot.repo / ".env"
    env.write_text("API_KEY=mine\nSITE_HOST=research.example.test\n")
    env.chmod(0o600)
    r = boot.run(LAB_SUBNET=LAB)
    assert r.code == 0, r.stderr
    assert actions(r)["env"]["action"] == "keep"
    assert env.read_text() == "API_KEY=mine\nSITE_HOST=research.example.test\n"
    assert env_values(boot.caddy / ".env")["SITE_HOST"] == "research.example.test"


# ---- the existing Caddy install --------------------------------------------------------------


@pytest.fixture
def installed(boot: Boot) -> Boot:
    """A VM with Caddy installed by hand: synced files, its own .env, a root.crt, extra files."""
    (boot.repo / ".env").write_text("API_KEY=k\nSITE_HOST=research.lab.example.test\n")
    boot.sync_caddy()
    cenv = boot.caddy / ".env"
    cenv.write_text(
        f"# owner's notes\nSITE_HOST=research.lab.example.test\nLAB_SUBNET={LAB}\n"
        f"TOOLBOX_HOST={TOOLBOX}\n"
    )
    cenv.chmod(0o600)
    (boot.caddy / "root.crt").write_text("CERT")
    (boot.caddy / "root.crt").chmod(0o600)
    (boot.caddy / "sites" / "other-tool.caddy").write_text("other {}\n")
    return boot


def test_existing_caddy_env_keeps_lab_values_and_needs_no_lab_subnet(installed: Boot) -> None:
    cenv_before = (installed.caddy / ".env").read_bytes()
    r = installed.run()
    assert r.code == 0, r.stderr
    a = actions(r)
    assert a["caddy_env"]["action"] == "keep" and a["caddy_files"]["action"] == "keep"
    assert a["caddy"]["action"] == "up"
    assert (installed.caddy / ".env").read_bytes() == cenv_before
    assert (installed.caddy / "root.crt").read_text() == "CERT"
    assert (installed.caddy / "sites" / "other-tool.caddy").exists()


def test_site_host_drift_rewrites_only_site_host_and_recreates(installed: Boot) -> None:
    (installed.repo / ".env").write_text("API_KEY=k\nSITE_HOST=research.new.example.test\n")
    r = installed.run(LAB_SUBNET="192.0.2.0/24")  # ignored: the Caddy env already has one
    assert r.code == 0, r.stderr
    a = actions(r)
    assert a["caddy_env"] == {"step": "caddy_env", "action": "update", "keys": ["SITE_HOST"]}
    assert a["caddy"]["action"] == "recreate"
    cenv = installed.caddy / ".env"
    assert cenv.read_text() == (
        f"# owner's notes\nSITE_HOST=research.new.example.test\nLAB_SUBNET={LAB}\n"
        f"TOOLBOX_HOST={TOOLBOX}\n"
    )
    assert mode(cenv) == 0o600
    assert "research.new.example.test" not in r.stdout + r.stderr and LAB not in r.stdout + r.stderr
    assert [c for c in installed.calls("") if " up " in f" {c} "] == [
        "compose up -d --wait --force-recreate"
    ]


def test_existing_caddy_env_without_lab_subnet(installed: Boot) -> None:
    cenv = installed.caddy / ".env"
    cenv.write_text("SITE_HOST=research.lab.example.test\nLAB_SUBNET=\n")
    r = installed.run()
    assert r.code == 2 and "LAB_SUBNET" in r.last["error"]
    r = installed.run(LAB_SUBNET=LAB)
    assert r.code == 0, r.stderr
    assert env_values(cenv)["LAB_SUBNET"] == LAB
    assert actions(r)["caddy_env"]["keys"] == ["LAB_SUBNET"]


def test_caddyfile_change_recreates(installed: Boot) -> None:
    (installed.caddy / "Caddyfile").write_text("# old\n")
    r = installed.run()
    assert r.code == 0, r.stderr
    a = actions(r)
    assert a["caddy_files"]["changed"] == ["Caddyfile"] and a["caddy"]["action"] == "recreate"
    assert (installed.caddy / "Caddyfile").read_bytes() == (
        installed.repo / "deploy/caddy/Caddyfile"
    ).read_bytes()
    calls = installed.calls("")
    assert "compose up -d --wait --force-recreate" in calls
    assert not any("reload" in c for c in calls)


def test_sites_only_change_reloads(installed: Boot) -> None:
    (installed.caddy / "sites" / "research-engine.caddy").write_text("# old\n")
    (installed.caddy / "README.md").write_text("old\n")
    r = installed.run()
    assert r.code == 0, r.stderr
    a = actions(r)
    assert a["caddy_files"]["changed"] == ["README.md", "sites/research-engine.caddy"]
    assert a["caddy"]["action"] == "reload"
    calls = installed.calls("")
    assert "compose up -d --wait" in calls
    assert "compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile" in calls
    assert not any("force-recreate" in c for c in calls)
    assert (installed.caddy / "sites" / "other-tool.caddy").exists()


def test_docs_only_change_neither_reloads_nor_recreates(installed: Boot) -> None:
    (installed.caddy / "README.md").write_text("old\n")
    r = installed.run()
    assert r.code == 0, r.stderr
    assert actions(r)["caddy"]["action"] == "up"
    assert not any("reload" in c or "force-recreate" in c for c in installed.calls(""))


def test_caddy_not_running_is_started_not_reloaded(installed: Boot) -> None:
    (installed.caddy / "sites" / "research-engine.caddy").write_text("# old\n")
    r = installed.run(FAKE_CADDY_DOWN="1")
    assert r.code == 0, r.stderr
    assert actions(r)["caddy"] == {"step": "caddy", "action": "up", "running": False}
    assert not any("reload" in c for c in installed.calls(""))


def test_timer_disabled_but_units_current_only_enables(installed: Boot) -> None:
    assert installed.run().code == 0
    Path(installed.extra["FAKE_TIMER_STATE"]).unlink()
    sudo_before = len(installed.calls("sudo"))
    r = installed.run()
    assert r.code == 0 and actions(r)["systemd"]["action"] == "enable"
    assert installed.calls("sudo")[sudo_before:] == [
        "systemctl daemon-reload",
        "systemctl enable --now research-engine-backup.timer",
    ]
