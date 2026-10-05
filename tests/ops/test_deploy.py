"""`make deploy` and `make rollback`: worktree-based, gated, auto-rollback (V1-22, §10).

Each test builds a throwaway git repo inside the fake-bin fixture's repo directory. Docker is the
fake from conftest (its `up` succeeds unless FAKE_UP_FAIL_SHA matches the GIT_SHA it is run with);
smoke and sandbox are fake scripts selected via SMOKE_CMD / SANDBOX_CMD that fail for one sha.
"""

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from .conftest import FakeEnv, Result
from .test_readonly_ops import json_lines

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
}
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

FAKE_CHECK = """#!/usr/bin/env bash
# fake {name}: logs what it ran against; fails when GIT_SHA equals ${var}
printf '%s %s\\n' "$GIT_SHA" "$DEPLOY_DIR" >> "{log}"
echo "fake {name} on $GIT_SHA" >&2
if [ -n "${{{var}:-}}" ] && [ "$GIT_SHA" = "${var}" ]; then
  echo '{{"ok": false, "command": "{name}", "error": "{name} failed"}}'
  exit 1
fi
echo '{{"ok": true, "command": "{name}"}}'
"""


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout.strip()


def commit(repo: Path, content: str) -> str:
    (repo / "app.txt").write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", content)
    return git(repo, "rev-parse", "--short=12", "HEAD")


class Deployer:
    def __init__(self, fake_env: FakeEnv, tmp_path: Path) -> None:
        self.env = fake_env
        self.repo = fake_env.repo
        self.log = self.repo / "deploys.jsonl"
        self.log.unlink()  # the fixture's legacy entries are not used here
        (self.repo / ".gitignore").write_text(".env\n.deploy/\ndeploys.jsonl\nbackups/\n")
        git(self.repo, "init", "-q", "-b", "main")
        self.checks: dict[str, Path] = {}
        for name in ("smoke", "sandbox"):
            script = tmp_path / f"fake_{name}.sh"
            log = tmp_path / f"{name}.log"
            var = f"FAIL_{name.upper()}_SHA"
            script.write_text(FAKE_CHECK.format(name=name, var=var, log=log))
            script.chmod(0o755)
            self.checks[name] = log
        self.smoke_cmd = str(tmp_path / "fake_smoke.sh")
        self.sandbox_cmd = str(tmp_path / "fake_sandbox.sh")

    def run(self, cmd: str, **env: str) -> Result:
        r = self.env.run(cmd, SMOKE_CMD=self.smoke_cmd, SANDBOX_CMD=self.sandbox_cmd, **env)
        assert len(json_lines(r.stdout)) == 1, (r.stdout, r.stderr)
        assert r.stdout.strip().splitlines()[-1] == json_lines(r.stdout)[0]
        return r

    def entries(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        out = [json.loads(line) for line in self.log.read_text().splitlines()]
        for e in out:
            assert TS.match(str(e["ts"])), e
        return out

    def current(self) -> str:
        link = self.repo / ".deploy" / "current"
        assert link.is_symlink()
        return git(link.resolve(), "rev-parse", "--short=12", "HEAD")

    def snapshot(self) -> tuple[str, str, str, str]:
        return (
            git(self.repo, "rev-parse", "HEAD"),
            git(self.repo, "symbolic-ref", "HEAD"),
            git(self.repo, "status", "--porcelain"),
            (self.repo / "app.txt").read_text(),
        )

    def up_shas(self) -> list[str]:
        """GIT_SHA seen by each fake `docker compose up`, in order."""
        f = self.env.calls.with_name("calls.log.up")
        return f.read_text().split() if f.exists() else []

    def up_calls(self) -> list[str]:
        return [c for c in self.env.docker_calls() if " up -d " in c]


@pytest.fixture
def dep(fake_env: FakeEnv, tmp_path: Path) -> Deployer:
    return Deployer(fake_env, tmp_path)


# ---- deploy: preconditions -------------------------------------------------------------------


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_deploy_refuses_dirty_tree(dep: Deployer, change: str) -> None:
    commit(dep.repo, "v1")
    if change == "tracked":
        (dep.repo / "app.txt").write_text("edited")
    else:
        (dep.repo / "new.txt").write_text("new")
    r = dep.run("deploy")
    assert r.code == 2
    assert r.last["ok"] is False
    assert r.last["error"] == "working tree has uncommitted changes"
    assert dep.env.docker_calls() == [] and dep.entries() == []
    assert not (dep.repo / ".deploy" / "current").exists()


def test_deploy_ignores_ignored_files(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    (dep.repo / "backups" / "more.sqlite").write_text("x")  # ignored: not "dirty"
    assert dep.run("deploy").code == 0


def test_deploy_refuses_while_another_holds_the_lock(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    (dep.repo / ".deploy").mkdir()
    with (dep.repo / ".deploy" / "lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for cmd in ("deploy", "rollback"):
            r = dep.run(cmd)
            assert r.code == 2 and r.last["ok"] is False
            assert "another deploy or rollback is running" in str(r.last["error"])
    assert dep.up_calls() == [] and dep.entries() == []


# ---- deploy: success -------------------------------------------------------------------------


def test_successful_deploy_appends_ok(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    before = dep.snapshot()
    r = dep.run("deploy")
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.last["ok"] is True and r.last["command"] == "deploy" and r.last["to"] == c1
    assert len(c1) == 12  # fixed short length: image tag, worktree name and log agree
    wt = dep.repo / ".deploy" / c1
    assert (wt / "app.txt").read_text() == "v1"
    assert dep.current() == c1
    assert (dep.repo / ".deploy" / "current").resolve() == wt.resolve()
    # bring-up: from the worktree, image tag = the short sha, waits for health
    [up] = dep.up_calls()
    assert "-p research-engine " in up and f"--project-directory {wt}" in up
    assert f"-f {wt}/compose.yaml" in up and f"--env-file {dep.repo}/.env" in up
    assert up.endswith("up -d --build --wait --wait-timeout 300")
    assert dep.up_shas() == [c1]
    # cwd = worktree too: compose may read security_opt's relative seccomp path from the cwd
    assert dep.env.calls.with_name("calls.log.up_pwd").read_text().split() == [str(wt)]
    # smoke, then sandbox, both against the new worktree
    for name in ("smoke", "sandbox"):
        assert dep.checks[name].read_text().split() == [c1, str(wt)]
    [entry] = dep.entries()
    assert entry["action"] == "deploy" and entry["to"] == c1 and entry["result"] == "ok"
    assert entry["smoke"] == "pass" and entry["sandbox"] == "pass"
    assert dep.snapshot() == before
    for secret in dep.env.secrets:
        assert secret not in r.stdout + r.stderr + dep.log.read_text()


def test_first_deploy_seeds_from_running_version(dep: Deployer) -> None:
    """No deploys.jsonl yet: `from` is the running app's /version git_sha (informational)."""
    c1 = commit(dep.repo, "v1")
    r = dep.run("deploy")
    assert r.code == 0
    assert r.last["from"] == "abc1234"  # the fake app's /version
    [entry] = dep.entries()
    assert entry["from"] == "abc1234" and entry["from_source"] == "running" and entry["to"] == c1


def test_deploy_from_is_last_ok_to(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    r = dep.run("deploy")
    assert r.code == 0 and r.last["from"] == c1 and r.last["to"] == c2
    assert [(e["from"], e["to"]) for e in dep.entries()][1] == (c1, c2)
    assert "from_source" not in dep.entries()[1]
    assert dep.current() == c2


def test_redeploying_same_commit_reuses_worktree(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    assert dep.run("deploy").code == 0
    assert dep.current() == c1
    assert [e["result"] for e in dep.entries()] == ["ok", "ok"]


# ---- deploy: failure and automatic rollback -------------------------------------------------


@pytest.mark.parametrize(
    ("fail_env", "smoke", "sandbox"),
    [
        ("FAIL_SMOKE_SHA", "fail", "skipped"),
        ("FAIL_SANDBOX_SHA", "pass", "fail"),
        ("FAKE_UP_FAIL_SHA", "skipped", "skipped"),
    ],
)
def test_failed_deploy_rolls_back_to_previous_good(
    dep: Deployer, fail_env: str, smoke: str, sandbox: str
) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    before = dep.snapshot()
    r = dep.run("deploy", **{fail_env: c2})
    assert r.code == 1, (r.stdout, r.stderr)
    assert r.last["ok"] is False and r.last["command"] == "deploy"
    assert r.last["rolled_back_to"] == c1 and r.last["rollback_ok"] is True
    # rollback branch at the old sha, current points at the old sha's worktree
    branches = git(dep.repo, "branch", "--list", "rollback/*", "--format=%(refname:short)")
    [branch] = branches.splitlines()
    assert re.fullmatch(r"rollback/\d{8}T\d{6}Z", branch)
    assert git(dep.repo, "rev-parse", "--short=12", branch) == c1
    assert dep.current() == c1
    # two bring-ups after the first deploy: the failed one, then the old sha
    assert dep.up_shas() == [c1, c2, c1]
    # both entries recorded
    _, failed, rolled = dep.entries()
    assert failed["action"] == "deploy" and failed["result"] == "failed"
    assert (failed["from"], failed["to"]) == (c1, c2)
    assert failed["smoke"] == smoke and failed["sandbox"] == sandbox
    assert failed["detail"]
    assert rolled["action"] == "rollback" and rolled["result"] == "ok"
    assert (rolled["from"], rolled["to"]) == (c2, c1) and rolled["smoke"] == "pass"
    assert rolled["branch"] == branch
    # the main working tree is untouched
    assert dep.snapshot() == before


def test_failed_smoke_detail_is_recorded(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    dep.run("deploy", FAIL_SMOKE_SHA=c2)
    failed = dep.entries()[1]
    detail = failed["detail"]
    assert isinstance(detail, dict) and detail["error"] == "smoke failed"


def test_failed_first_deploy_leaves_stack_up(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    before = dep.snapshot()
    r = dep.run("deploy", FAIL_SMOKE_SHA=c1)
    assert r.code == 1 and r.last["ok"] is False and r.last["rolled_back_to"] is None
    assert r.last["from"] == "abc1234"  # seeded, but never a rollback target (no worktree)
    assert len(dep.up_calls()) == 1
    assert dep.current() == c1  # failed stack left up for diagnosis
    [entry] = dep.entries()
    assert entry["result"] == "failed" and entry["smoke"] == "fail"
    assert git(dep.repo, "branch", "--list", "rollback/*") == ""
    assert dep.snapshot() == before


def test_failed_redeploy_of_same_sha_does_not_roll_back_to_itself(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    r = dep.run("deploy", FAIL_SMOKE_SHA=c1)
    assert r.code == 1 and r.last["rolled_back_to"] is None
    assert len(dep.up_calls()) == 2


# ---- rollback.sh ----------------------------------------------------------------------------


def test_rollback_defaults_to_last_good_other_than_current(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    before = dep.snapshot()
    r = dep.run("rollback")
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.last["ok"] is True and r.last["command"] == "rollback"
    assert (r.last["from"], r.last["to"]) == (c2, c1)
    assert dep.current() == c1
    branch = str(r.last["branch"])
    assert git(dep.repo, "rev-parse", "--short=12", branch) == c1
    entry = dep.entries()[-1]
    assert entry["action"] == "rollback" and entry["result"] == "ok"
    assert (entry["from"], entry["to"]) == (c2, c1) and entry["smoke"] == "pass"
    assert dep.checks["sandbox"].read_text().splitlines()[-1].split()[0] == c1
    assert dep.snapshot() == before
    # rolling back again goes forward to the other good sha
    r2 = dep.run("rollback")
    assert r2.code == 0 and r2.last["to"] == c2 and dep.current() == c2
    assert dep.snapshot() == before


def test_rollback_to_explicit_sha(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback", SHA=c1)
    assert r.code == 0 and r.last["to"] == c1 and dep.current() == c1


@pytest.mark.parametrize("sha", ["deadbee", "not-a-sha;id", "HEAD"])
def test_rollback_rejects_unknown_sha(dep: Deployer, sha: str) -> None:
    commit(dep.repo, "v1")
    r = dep.run("rollback", SHA=sha)
    assert r.code == 2 and r.last["ok"] is False
    assert dep.up_calls() == [] and dep.entries() == []


def test_rollback_without_candidate_is_a_precondition_error(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback")
    assert r.code == 2 and r.last["ok"] is False
    assert "no previous good deploy" in str(r.last["error"])
    assert len(dep.up_calls()) == 1


def test_failed_rollback_is_recorded(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback", FAIL_SMOKE_SHA=c1)
    assert r.code == 1 and r.last["ok"] is False and r.last["to"] == c1
    entry = dep.entries()[-1]
    assert entry["action"] == "rollback" and entry["result"] == "failed"


# ---- retention -------------------------------------------------------------------------------


def test_prunes_worktrees_and_images_by_explicit_tag(dep: Deployer) -> None:
    shas = []
    for i in range(5):
        shas.append(commit(dep.repo, f"v{i}"))
        tags = " ".join([*shas, "dev", "abc1234", "latest"])
        r = dep.run(
            "deploy",
            FAKE_IMAGE_TAGS=tags,
            FAKE_PS_IMAGES="research-engine-app:abc1234 caddy:2",
        )
        assert r.code == 0, (r.stdout, r.stderr)
    before = dep.snapshot()
    deploy_dir = dep.repo / ".deploy"
    kept = sorted(p.name for p in deploy_dir.iterdir() if p.is_dir() and not p.is_symlink())
    assert kept == sorted(shas[1:])  # current + the 3 before it
    listed = git(dep.repo, "worktree", "list", "--porcelain")
    assert str(deploy_dir / shas[0]) not in listed
    assert all(str(deploy_dir / s) in listed for s in shas[1:])
    calls = dep.env.docker_calls()
    rms = [c for c in calls if c.startswith("image rm")]
    # c0 is first removable once 4 newer ones exist; abc1234 is in use; dev/latest are not shas
    assert rms == [f"image rm research-engine-app:{shas[0]}"]
    assert not any("prune" in c.split() for c in calls)
    assert dep.snapshot() == before


def test_makefile_passes_sha_via_environment() -> None:
    from .conftest import ROOT

    evil = 'a"; echo INJECTED; "$(id)`id`'
    out = subprocess.run(
        ["make", "-n", "rollback", f"SHA={evil}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "INJECTED" not in out and "uid=" not in out
    text = (ROOT / "Makefile").read_text()
    assert re.search(r"^export .*\bSHA\b", text, re.M)


def test_seeded_running_image_is_kept_until_it_ages_out(dep: Deployer) -> None:
    """The pre-worktree image (seeded `from`) counts as the oldest deployed sha."""
    shas: list[str] = []
    rms_after: list[list[str]] = []
    for i in range(4):
        shas.append(commit(dep.repo, f"v{i}"))
        r = dep.run("deploy", FAKE_IMAGE_TAGS=" ".join(["abc1234", *shas]))
        assert r.code == 0, (r.stdout, r.stderr)
        rms_after.append([c for c in dep.env.docker_calls() if c.startswith("image rm")])
    assert rms_after[2] == []  # current + 3 older: c1, c0 and the seeded abc1234
    assert rms_after[3] == ["image rm research-engine-app:abc1234"]


# ---- fix round 1: explicit error checks, verified worktree reuse --------------------------


def assert_current_is_worktree(dep: Deployer, sha: str) -> None:
    link = dep.repo / ".deploy" / "current"
    target = link.resolve()
    assert target.is_dir() and (target / ".git").is_file(), "current must be a real worktree"
    assert git(target, "rev-parse", "--show-toplevel") == str(target)
    assert dep.current() == sha


def test_auto_rollback_to_a_pruned_worktree_recreates_it_on_the_branch(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    git(dep.repo, "worktree", "remove", str(dep.repo / ".deploy" / c1))  # as retention would
    c2 = commit(dep.repo, "v2")
    r = dep.run("deploy", FAIL_SMOKE_SHA=c2)
    assert r.code == 1 and r.last["rolled_back_to"] == c1 and r.last["rollback_ok"] is True
    assert_current_is_worktree(dep, c1)
    wt = dep.repo / ".deploy" / c1
    assert git(wt, "symbolic-ref", "--short", "HEAD") == r.last["rollback_branch"]
    assert dep.up_shas() == [c1, c2, c1]


def test_failure_switching_to_rollback_target_keeps_current_usable(dep: Deployer) -> None:
    """The target is still registered as a worktree but its directory is gone: `worktree add`
    refuses. The rollback must fail cleanly, never leave a file at .deploy/<sha> or repoint
    `current` at anything but a verified worktree."""
    import shutil

    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    shutil.rmtree(dep.repo / ".deploy" / c1)
    c2 = commit(dep.repo, "v2")
    before = dep.snapshot()
    r = dep.run("deploy", FAIL_SMOKE_SHA=c2)
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["rolled_back_to"] is None and r.last["rollback_ok"] is False
    assert r.last["rollback_target"] == c1 and r.last["current"] == c2
    assert not (dep.repo / ".deploy" / c1).exists(), "no placeholder at .deploy/<sha>"
    assert_current_is_worktree(dep, c2)
    assert dep.up_shas() == [c1, c2]  # never brought up from a non-worktree
    _, failed, rolled = dep.entries()
    assert failed["result"] == "failed" and failed["to"] == c2
    assert rolled["action"] == "rollback" and rolled["result"] == "failed"
    assert rolled["to"] == c1 and rolled["smoke"] == "skipped"
    detail = rolled["detail"]
    assert isinstance(detail, dict) and detail["step"] == "worktree"
    assert dep.snapshot() == before
    # the ops tools still work afterwards
    assert dep.run("status").code == 0
    r2 = dep.run("rollback")
    assert r2.code == 1 and r2.last["ok"] is False and r2.last["command"] == "rollback"
    assert_current_is_worktree(dep, c2)


def test_dirty_rollback_target_worktree_fails_the_rollback_not_the_tools(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    (dep.repo / ".deploy" / c1 / "app.txt").write_text("edited in the worktree")
    c2 = commit(dep.repo, "v2")
    r = dep.run("deploy", FAIL_SMOKE_SHA=c2)
    assert r.code == 1, (r.stdout, r.stderr)
    assert r.last["rolled_back_to"] is None and r.last["rollback_ok"] is False
    assert r.last["rollback_target"] == c1
    assert "not a clean checkout" in str(r.last["rollback_error"])
    assert (dep.repo / ".deploy" / c1 / "app.txt").read_text() == "edited in the worktree"
    assert_current_is_worktree(dep, c2)
    rolled = dep.entries()[-1]
    assert rolled["action"] == "rollback" and rolled["result"] == "failed"


def test_empty_directory_at_worktree_path_is_replaced(dep: Deployer) -> None:
    """An empty .deploy/<sha> (e.g. a killed `worktree add`) resolves to the main repo under
    `git -C`; it must never be accepted as the worktree. Being empty, it is safely replaced."""
    c1 = commit(dep.repo, "v1")
    (dep.repo / ".deploy" / c1).mkdir(parents=True)
    r = dep.run("deploy")
    assert r.code == 0, (r.stdout, r.stderr)
    assert_current_is_worktree(dep, c1)
    assert (dep.repo / ".deploy" / c1 / "app.txt").read_text() == "v1"


def test_non_worktree_directory_at_worktree_path_is_refused(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    (dep.repo / ".deploy" / c1).mkdir(parents=True)
    (dep.repo / ".deploy" / c1 / "stray.txt").write_text("x")
    before = dep.snapshot()
    r = dep.run("deploy")
    assert r.code == 1 and r.last["ok"] is False and r.last["rolled_back_to"] is None
    assert r.last["stack_changed"] is False
    assert "not a clean checkout" in str(r.last["error"])
    assert dep.up_calls() == []
    assert not (dep.repo / ".deploy" / "current").exists()
    assert (dep.repo / ".deploy" / c1 / "stray.txt").read_text() == "x"
    [entry] = dep.entries()
    assert entry["result"] == "failed" and entry["smoke"] == "skipped"
    assert dep.snapshot() == before


def test_rollback_sha_prefix_is_normalised(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback", SHA=c1[:7])
    assert r.code == 0 and r.last["to"] == c1
    assert_current_is_worktree(dep, c1)
    assert dep.up_shas()[-1] == c1


def test_no_candidate_rollback_suggests_the_running_sha(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy", FAIL_SMOKE_SHA=c1).code == 1  # first deploy fails, seeded abc1234
    r = dep.run("rollback")
    assert r.code == 2 and "SHA=abc1234" in str(r.last["error"])
    assert r.last["suggested_sha"] == "abc1234"


def test_cleanup_failures_never_fail_a_successful_deploy(dep: Deployer) -> None:
    shas = [commit(dep.repo, f"v{i}") for i in range(1)]
    for i in range(1, 5):
        assert dep.run("deploy").code == 0
        shas.append(commit(dep.repo, f"v{i}"))
    r = dep.run("deploy", FAKE_IMAGE_TAGS=" ".join(shas), FAKE_IMAGE_RM_FAIL="1")
    assert r.code == 0 and r.last["ok"] is True, (r.stdout, r.stderr)
    assert "warning" in r.stderr.lower()


def test_claude_temp_settings_files_are_ignored() -> None:
    from .conftest import ROOT

    p = subprocess.run(
        ["git", "check-ignore", "-q", ".claude/settings.local.json.tmp.20589.bc07ca4332b9"],
        cwd=ROOT,
    )
    assert p.returncode == 0
    q = subprocess.run(["git", "check-ignore", "-q", ".claude/settings.json"], cwd=ROOT)
    assert q.returncode == 1, "shared settings stay tracked"
