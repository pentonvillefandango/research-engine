"""`make backup` and `make restore` (V1-22, §10) against the fake docker from conftest."""

import fcntl
import os
import re
import stat
import time
from pathlib import Path

import pytest

from .conftest import FakeEnv, Result
from .test_readonly_ops import json_lines

DAY = 86400


def one_line(r: Result) -> None:
    assert len(json_lines(r.stdout)) == 1, (r.stdout, r.stderr)
    assert r.stdout.strip().splitlines()[-1] == json_lines(r.stdout)[0]


def age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t), follow_symlinks=False)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---- backup ----------------------------------------------------------------------------------


def test_backup_copies_out_and_removes_the_container_temp(fake_env: FakeEnv) -> None:
    r = fake_env.run("backup")
    one_line(r)
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["command"] == "backup" and r.last["pruned"] == []
    out = Path(str(r.last["file"]))
    m = re.fullmatch(r"research-engine-(\d{8}T\d{6}Z)\.sqlite", out.name)
    assert m and out.parent == fake_env.repo / "backups"
    assert r.last["bytes"] == out.stat().st_size > 0
    assert mode(out) == 0o600 and mode(out.parent) == 0o700
    calls = fake_env.docker_calls()
    # a per-run temp name (stamp + pid): concurrent runs never share or delete each other's file
    inner = next(
        w
        for c in calls
        for w in c.split()
        if re.fullmatch(rf"/data/\.backup-{m.group(1)}-\d+\.sqlite", w)
    )
    i_backup = next(
        i for i, c in enumerate(calls) if f"research-engine db backup --out {inner}" in c
    )
    i_cp = next(i for i, c in enumerate(calls) if f" cp app:{inner} {out}.part" in f" {c}")
    i_rm = next(i for i, c in enumerate(calls) if f"rm -f {inner}" in c)
    assert i_backup < i_cp < i_rm
    assert all(c.startswith("compose -p research-engine ") for c in calls)
    assert " exec -T app " in f" {calls[i_backup]} "
    assert not list(out.parent.glob("*.part"))
    # stale temps of killed runs are swept from the volume, after this run's own file is gone
    i_sweep = next(i for i, c in enumerate(calls) if "sweep-stale-backup-temps" in c)
    assert i_rm < i_sweep


def hold_lock(repo: Path) -> int:
    (repo / ".deploy").mkdir(exist_ok=True)
    fd = os.open(repo / ".deploy" / "lock", os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_backup_waits_for_the_ops_lock_and_gives_up(fake_env: FakeEnv) -> None:
    fd = hold_lock(fake_env.repo)
    try:
        r = fake_env.run("backup", BACKUP_LOCK_WAIT="0")
        one_line(r)
        assert r.code == 2 and "lock" in r.last["error"]
        assert fake_env.docker_calls() == []
        # a caller that already holds the lock (restore) says so
        r = fake_env.run("backup", BACKUP_LOCK_WAIT="0", OPS_LOCK_HELD="1")
        assert r.code == 0, r.stderr
    finally:
        os.close(fd)


def test_backup_tag_names_the_file_and_skips_retention(fake_env: FakeEnv) -> None:
    bdir = fake_env.repo / "backups"
    (bdir / "research-engine-a.sqlite").write_bytes(b"x")
    age(bdir / "research-engine-a.sqlite", 30)
    r = fake_env.run("backup", "--tag", "prerestore")
    assert r.code == 0, r.stderr
    assert re.fullmatch(
        r"research-engine-\d{8}T\d{6}Z-prerestore\.sqlite", Path(str(r.last["file"])).name
    )
    assert r.last["pruned"] == [] and (bdir / "research-engine-a.sqlite").exists()


@pytest.mark.parametrize("args", [["--tag"], ["--tag", "Bad/Tag"], ["--bogus"]])
def test_backup_rejects_bad_arguments(fake_env: FakeEnv, args: list[str]) -> None:
    r = fake_env.run("backup", *args)
    assert r.code == 2 and r.last["ok"] is False and fake_env.docker_calls() == []


def test_backup_same_second_name_clash_is_refused(fake_env: FakeEnv) -> None:
    first = fake_env.run("backup")
    assert first.code == 0
    clash = Path(str(first.last["file"]))
    r = fake_env.run("backup", BACKUP_TS=clash.name[len("research-engine-") : -len(".sqlite")])
    assert r.code == 2 and "exists" in r.last["error"]
    assert clash.exists()


def test_backup_creates_a_private_backup_dir(fake_env: FakeEnv, tmp_path: Path) -> None:
    bdir = tmp_path / "new" / "backups"
    r = fake_env.run("backup", BACKUP_DIR=str(bdir))
    assert r.code == 0, r.stderr
    assert mode(bdir) == 0o700 and Path(str(r.last["file"])).parent == bdir


def test_backup_retention_prunes_only_old_matching_files(fake_env: FakeEnv) -> None:
    bdir = fake_env.repo / "backups"
    keep_old = ["old.sqlite", "other-2026.sqlite", "research-engine-x.sqlite.part", "notes.txt"]
    for name in keep_old:
        (bdir / name).write_bytes(b"x")
        age(bdir / name, 30)
    (bdir / "research-engine-20260901T033000Z.sqlite").write_bytes(b"old")
    age(bdir / "research-engine-20260901T033000Z.sqlite", 20)
    (bdir / "research-engine-20260925T033000Z.sqlite").write_bytes(b"new")
    age(bdir / "research-engine-20260925T033000Z.sqlite", 10)
    (bdir / "sub").mkdir()
    (bdir / "sub" / "research-engine-nested.sqlite").write_bytes(b"x")
    age(bdir / "sub" / "research-engine-nested.sqlite", 30)
    (bdir / "research-engine-dir.sqlite").mkdir()
    age(bdir / "research-engine-dir.sqlite", 30)
    outside = fake_env.repo / "precious.sqlite"
    outside.write_bytes(b"x")
    (bdir / "research-engine-link.sqlite").symlink_to(outside)
    age(bdir / "research-engine-link.sqlite", 30)

    r = fake_env.run("backup")
    assert r.code == 0, r.stderr
    assert r.last["pruned"] == ["research-engine-20260901T033000Z.sqlite"]
    assert not (bdir / "research-engine-20260901T033000Z.sqlite").exists()
    for name in [
        *keep_old,
        "research-engine-20260925T033000Z.sqlite",
        "research-engine-dir.sqlite",
    ]:
        assert (bdir / name).exists(), name
    assert (bdir / "sub" / "research-engine-nested.sqlite").exists()
    assert (bdir / "research-engine-link.sqlite").is_symlink() and outside.exists()
    assert Path(str(r.last["file"])).exists()


def test_backup_retention_days_is_configurable(fake_env: FakeEnv) -> None:
    bdir = fake_env.repo / "backups"
    (bdir / "research-engine-a.sqlite").write_bytes(b"x")
    age(bdir / "research-engine-a.sqlite", 3)
    r = fake_env.run("backup", BACKUP_RETENTION_DAYS="2")
    assert r.code == 0 and r.last["pruned"] == ["research-engine-a.sqlite"]


@pytest.mark.parametrize("days", ["0", "-1", "abc", "1.5", ""])
def test_backup_rejects_bad_retention(fake_env: FakeEnv, days: str) -> None:
    r = fake_env.run("backup", BACKUP_RETENTION_DAYS=days)
    one_line(r)
    assert r.code == 2 and r.last["ok"] is False
    assert fake_env.docker_calls() == []


def test_backup_in_container_failure_keeps_everything(fake_env: FakeEnv) -> None:
    bdir = fake_env.repo / "backups"
    (bdir / "research-engine-a.sqlite").write_bytes(b"x")
    age(bdir / "research-engine-a.sqlite", 30)
    r = fake_env.run("backup", FAKE_DB_BACKUP_FAIL="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["detail"]["error"] == "database not found"
    assert (bdir / "research-engine-a.sqlite").exists()  # no retention after a failed backup
    assert sorted(p.name for p in bdir.iterdir()) == ["old.sqlite", "research-engine-a.sqlite"]
    assert not any(" cp " in f" {c} " for c in fake_env.docker_calls())


def test_backup_copy_failure_cleans_up(fake_env: FakeEnv) -> None:
    r = fake_env.run("backup", FAKE_CP_FAIL="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False
    assert any("rm -f /data/.backup-" in c for c in fake_env.docker_calls())
    assert sorted(p.name for p in (fake_env.repo / "backups").iterdir()) == ["old.sqlite"]


def test_backup_never_prints_secrets(fake_env: FakeEnv) -> None:
    r = fake_env.run("backup")
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr


# ---- restore ---------------------------------------------------------------------------------


@pytest.fixture
def backup_file(fake_env: FakeEnv) -> Path:
    f = fake_env.repo / "backups" / "research-engine-20261005T033000Z.sqlite"
    f.write_bytes(b"SQLite format 3 the backup")
    f.chmod(0o600)
    (fake_env.repo / "backups").chmod(0o700)
    return f


def _refusal_cases(repo: Path, tmp: Path) -> dict[str, str]:
    bdir = repo / "backups"
    outside = tmp / "elsewhere"
    outside.mkdir()
    (outside / "research-engine-x.sqlite").write_bytes(b"x")
    (repo / "research-engine-up.sqlite").write_bytes(b"x")
    (bdir / "research-engine-evil.sqlite").symlink_to(outside / "research-engine-x.sqlite")
    (bdir / "research-engine-d.sqlite").mkdir()
    (bdir / "research-engine-.sqlite").write_bytes(b"x")
    (bdir / "sub").mkdir()
    (bdir / "sub" / "research-engine-n.sqlite").write_bytes(b"x")
    return {
        "missing arg": "",
        "nonexistent": str(bdir / "research-engine-nope.sqlite"),
        "outside": str(outside / "research-engine-x.sqlite"),
        "bad name": str(bdir / "old.sqlite"),
        "symlink escaping": str(bdir / "research-engine-evil.sqlite"),
        "dotdot": str(bdir / ".." / "research-engine-up.sqlite"),
        "directory": str(bdir / "research-engine-d.sqlite"),
        "empty stamp": str(bdir / "research-engine-.sqlite"),
        "nested": str(bdir / "sub" / "research-engine-n.sqlite"),
    }


@pytest.mark.parametrize(
    "case",
    [
        "missing arg",
        "nonexistent",
        "outside",
        "bad name",
        "symlink escaping",
        "dotdot",
        "directory",
        "empty stamp",
        "nested",
    ],
)
def test_restore_refuses_bad_paths(fake_env: FakeEnv, tmp_path: Path, case: str) -> None:
    arg = _refusal_cases(fake_env.repo, tmp_path)[case]
    r = fake_env.run("restore", *([arg] if arg else []))
    one_line(r)
    assert r.code == 2 and r.last["ok"] is False and r.last["command"] == "restore"
    assert fake_env.docker_calls() == []  # the app was never stopped


def test_restore_relative_path_inside_backup_dir_is_accepted(
    fake_env: FakeEnv, backup_file: Path
) -> None:
    r = fake_env.run("restore", f"backups/{backup_file.name}")
    assert r.code == 0, (r.stdout, r.stderr)


def test_restore_stops_restores_starts_and_checks_health(
    fake_env: FakeEnv, backup_file: Path
) -> None:
    r = fake_env.run("restore", str(backup_file))
    one_line(r)
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.last["ok"] is True and r.last["restored"] == backup_file.name
    assert r.last["health"]["ok"] is True and r.last["health"]["command"] == "health"
    calls = fake_env.docker_calls()
    i_check = next(i for i, c in enumerate(calls) if " run " in f" {c} " and "--check" in c)
    i_safety = next(i for i, c in enumerate(calls) if "research-engine db backup" in c)
    i_stop = next(i for i, c in enumerate(calls) if c.endswith(" stop app"))
    i_run = next(i for i, c in enumerate(calls) if " run " in f" {c} " and "--check" not in c)
    i_start = next(i for i, c in enumerate(calls) if " start " in f" {c} ")
    assert i_check < i_safety < i_stop < i_run < i_start
    assert calls[i_check].endswith(" app research-engine db restore --check --from /restore.sqlite")
    # the pre-restore safety backup: retention and restore can find it, owner-only
    safety = Path(str(r.last["safety_backup"]))
    assert safety.parent == backup_file.parent and mode(safety) == 0o600
    assert re.fullmatch(r"research-engine-\d{8}T\d{6}Z-prerestore\.sqlite", safety.name)
    run = calls[i_run]
    assert " run --rm --no-deps -T " in f" {run} "
    assert run.endswith(" app research-engine db restore --from /restore.sqlite")
    assert re.search(r" -v \S+/restore\.sqlite:/restore\.sqlite:ro ", run)
    assert calls[i_start].endswith(" app")
    # the container user (10001) reads a 644 copy in a 700 staging dir inside BACKUP_DIR;
    # the backup itself stays 600 and the staging dir is gone afterwards
    mounts = {tuple(ln.split()) for ln in Path(f"{fake_env.calls}.run").read_text().splitlines()}
    assert len(mounts) == 1  # the check and the restore read the same staged copy
    mounted, fmode, dmode = mounts.pop()
    src = Path(mounted)
    assert (fmode, dmode) == ("644", "700")
    assert src.parent.parent == backup_file.parent and src != backup_file
    assert Path(f"{fake_env.calls}.run.copy").read_bytes() == backup_file.read_bytes()
    assert not src.parent.exists()
    assert mode(backup_file) == 0o600
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr


def test_restore_cli_failure_restarts_app_and_fails(fake_env: FakeEnv, backup_file: Path) -> None:
    r = fake_env.run("restore", str(backup_file), FAKE_RESTORE_FAIL="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False and r.last["restored"] is None
    assert r.last["detail"]["error"] == "missing tables: event"
    assert "still in place" in r.last["error"]
    assert any(" start " in f" {c} " for c in fake_env.docker_calls())
    assert not list(backup_file.parent.glob(".restore.*"))


def test_restore_replaced_but_run_failed_says_so(fake_env: FakeEnv, backup_file: Path) -> None:
    r = fake_env.run("restore", str(backup_file), FAKE_RUN_EXIT="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["restored"] == backup_file.name
    assert "still in place" not in r.last["error"] and "replaced" in r.last["error"]


def test_restore_invalid_backup_never_stops_the_app(fake_env: FakeEnv, backup_file: Path) -> None:
    r = fake_env.run("restore", str(backup_file), FAKE_CHECK_FAIL="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False and r.last["restored"] is None
    assert r.last["detail"]["error"] == "missing tables: event"
    calls = fake_env.docker_calls()
    assert not any(c.endswith(" stop app") or "db backup" in c for c in calls)
    assert not any(" start " in f" {c} " for c in calls)


def test_restore_safety_backup_failure_aborts_before_stopping(
    fake_env: FakeEnv, backup_file: Path
) -> None:
    r = fake_env.run("restore", str(backup_file), FAKE_DB_BACKUP_FAIL="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False and r.last["restored"] is None
    assert "safety backup" in r.last["error"]
    assert not any(c.endswith(" stop app") for c in fake_env.docker_calls())


def test_restore_refuses_a_backup_with_a_wal_file(fake_env: FakeEnv, backup_file: Path) -> None:
    Path(f"{backup_file}-wal").write_bytes(b"frames")
    r = fake_env.run("restore", str(backup_file))
    one_line(r)
    assert r.code == 2 and "-wal" in r.last["error"] and fake_env.docker_calls() == []


def test_restore_holds_the_ops_lock(fake_env: FakeEnv, backup_file: Path) -> None:
    fd = hold_lock(fake_env.repo)
    try:
        r = fake_env.run("restore", str(backup_file))
    finally:
        os.close(fd)
    assert r.code == 2 and "lock" in r.last["error"] and fake_env.docker_calls() == []


def test_restore_unhealthy_after_start_fails(fake_env: FakeEnv, backup_file: Path) -> None:
    r = fake_env.run("restore", str(backup_file), FAKE_HEALTH_FILE="exec_health_down.json")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["restored"] == backup_file.name and r.last["health"]["ok"] is False


def test_restore_stop_failure_does_not_restore(fake_env: FakeEnv, backup_file: Path) -> None:
    r = fake_env.run("restore", str(backup_file), FAKE_STOP_FAIL="1")
    one_line(r)
    assert r.code == 1 and r.last["ok"] is False
    assert not any(" run " in f" {c} " and "--check" not in c for c in fake_env.docker_calls())
