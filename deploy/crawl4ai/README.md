# deploy/crawl4ai

Files mounted read-only into the `crawl4ai` service so Chromium runs **with its sandbox on** (ADR-0022, REQUIREMENTS §12).

## `seccomp-chromium.json`

This is Docker's default seccomp profile with exactly **one rule appended** at the end of `syscalls`:

```json
{
	"names": ["clone", "unshare", "setns"],
	"action": "SCMP_ACT_ALLOW",
	"comment": "research-engine ADR-0022: Chromium namespace sandbox (CLONE_NEWUSER/NEWPID/NEWNET) without CAP_SYS_ADMIN"
}
```

- **Upstream base:** `seccomp/default.json` from https://github.com/moby/profiles, commit `6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef` (2026-09-17). The raw file at that commit has sha256 `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6`. It was checked against Docker Engine 29.6.2.
- **Why the rule is needed:** in the default profile, `clone` with namespace flags, `unshare` and `setns` are allowed only for containers that hold `CAP_SYS_ADMIN`. Chromium's namespace sandbox needs them to put the zygote and renderers in their own user, PID and net namespaces. Without the rule, Chromium aborts with `No usable sandbox!`.
- **Regenerate:**

  ```bash
  curl -fsSL https://raw.githubusercontent.com/moby/profiles/<commit>/seccomp/default.json -o /tmp/default.json
  python3 - <<'EOF'
  import json
  d = json.load(open("/tmp/default.json"))
  d["syscalls"].append({"names": ["clone", "unshare", "setns"], "action": "SCMP_ACT_ALLOW",
      "comment": "research-engine ADR-0022: Chromium namespace sandbox (CLONE_NEWUSER/NEWPID/NEWNET) without CAP_SYS_ADMIN"})
  open("deploy/crawl4ai/seccomp-chromium.json", "w").write(json.dumps(d, indent="\t") + "\n")
  EOF
  diff /tmp/default.json deploy/crawl4ai/seccomp-chromium.json   # only the appended rule (+ final newline)
  scripts/check_sandbox.sh                                       # must print "sandbox":"on"
  ```

## `sitecustomize.py`

This is an add-on that Python auto-imports. It is mounted at `/usr/local/lib/python3.12/site-packages/sitecustomize.py`.

Crawl4AI 0.9.4 forces `--no-sandbox` on every launch, even when `CRAWL4AI_CHROMIUM_SANDBOX=true` is set. The add-on wraps the launch paths so the flag is honoured. It lists the exact `file:line` injection points it neutralises in its header.

If the add-on can't find a patch target, it prints a `WARNING` to stderr and does not crash Crawl4AI. `scripts/check_sandbox.sh` then fails.

The mount path depends on the image's Python version (3.12). If an upgrade changes that version, the add-on silently stops loading, and `check_sandbox.sh` is what catches it.

**Delete it** once upstream honours the flag on every launch path. The upstream report is in `docs/upstream/crawl4ai-chromium-sandbox-issue.md`.

## Verify

`scripts/check_sandbox.sh` must print `{"sandbox":"on","no_sandbox_procs":0,...,"crawl_ok":true}`. It also runs as `tests/integration/test_sandbox_live.py`.
