# deploy/crawl4ai

Files mounted read-only into the `crawl4ai` service so Chromium runs **with its sandbox on** (ADR-0022, REQUIREMENTS §12).

## `seccomp-chromium.json`

This is Docker's default seccomp profile with exactly **one rule appended** at the end of `syscalls`:

```json
{
	"names": ["clone", "unshare"],
	"action": "SCMP_ACT_ALLOW",
	"comment": "research-engine ADR-0022: Chromium namespace sandbox (CLONE_NEWUSER/NEWPID/NEWNET) without CAP_SYS_ADMIN"
}
```

- **Upstream base:** `seccomp/default.json` from https://github.com/moby/profiles, commit `6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef` (2026-09-17). The raw file at that commit has sha256 `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6`. It was checked against Docker Engine 29.6.2.
- **Why the rule is needed:** in the default profile, `clone` with namespace flags and `unshare` are allowed only for containers that hold `CAP_SYS_ADMIN`. Chromium's namespace sandbox needs both to put the zygote and renderers in their own user, PID and net namespaces. Without them, Chromium aborts with `No usable sandbox!`.
- **What we tested (2026-10-04):**
  - `clone` alone fails, with `No usable sandbox!`.
  - `setns` is **not** needed: both launch paths pass `scripts/check_sandbox.sh` without it, so it was removed from the rule.
- **Residual risk:** see ADR-0022, "Residual risk".
- **Regenerate:**

  ```bash
  curl -fsSL https://raw.githubusercontent.com/moby/profiles/<commit>/seccomp/default.json -o /tmp/default.json
  python3 - <<'EOF'
  import json
  d = json.load(open("/tmp/default.json"))
  d["syscalls"].append({"names": ["clone", "unshare"], "action": "SCMP_ACT_ALLOW",
      "comment": "research-engine ADR-0022: Chromium namespace sandbox (CLONE_NEWUSER/NEWPID/NEWNET) without CAP_SYS_ADMIN"})
  open("deploy/crawl4ai/seccomp-chromium.json", "w").write(json.dumps(d, indent="\t") + "\n")
  EOF
  diff /tmp/default.json deploy/crawl4ai/seccomp-chromium.json   # only the appended rule (+ final newline)
  scripts/check_sandbox.sh dev     # dev stack; on the live stack: make sandbox. Must print "sandbox": "on"
  ```

## `addon/sitecustomize.py`

This is an add-on mounted read-only at `/opt/re-addon`, with `PYTHONPATH=/opt/re-addon`, so any Python version auto-imports it. Crawl4AI 0.9.4 forces `--no-sandbox` on every launch, even when `CRAWL4AI_CHROMIUM_SANDBOX=true` is set. The add-on makes the flag work as intended.

It installs only when `CRAWL4AI_CHROMIUM_SANDBOX=true` **and** `crawl4ai` is importable. That keeps it inert in supervisord's Debian Python, which sees `PYTHONPATH` too. It patches lazily on import:
- `playwright.async_api` and `patchright.async_api`: `BrowserType.launch` and `launch_persistent_context` strip the flag and force `chromium_sandbox=True`;
- `crawl4ai.browser_manager`: its `subprocess` becomes a shim whose `Popen` strips the flag from any argv whose argv0 contains `chrom`. This is the choke point for `browser_mode="builtin"` (`ManagedBrowser`). The `build_browser_flags` and `start` wrappers on top are extra protection.

Its header lists the exact `file:line` injection points.

On install it prints `[research-engine sandbox add-on] active (...)` to stderr. On any missing or odd-shaped target it prints `[research-engine sandbox add-on] WARNING: ...`. It never crashes Crawl4AI. `scripts/check_sandbox.sh` fails if the `active` line is missing or any `WARNING` appears in the container logs since start.

**Delete the `addon/` directory, the mount and `PYTHONPATH`** once upstream honours the flag on every launch path. The upstream report is in `docs/upstream/crawl4ai-chromium-sandbox-issue.md`.

## Verify

`scripts/check_sandbox.sh dev|prod` crawls once on the default launch path and once with `browser_mode=builtin`. Each crawl uses a per-run `viewport_width`, which forces a fresh launch. The crawl requests are sent from inside the crawl4ai container to `127.0.0.1:11235`, with that container's own token, so no published port is needed. `dev` checks the dev stack (project `research-engine-dev`); `prod` checks the live stack and runs as `make sandbox` (`ops/sandbox.sh`). It must print a final JSON line with:

```
{"ok": true, "command": "sandbox", "sandbox": "on", "no_sandbox_procs": 0, "default_ok": true, "builtin_ok": true, "addon_active": true, "addon_warnings": 0, "crawl_ok": true, ...}
```

The dev mode also runs as `tests/integration/test_sandbox_live.py`.
