# ADR-0022: Chromium sandbox stays on in Crawl4AI

## Status

Accepted (2026-10-04). Source: B3.

## Context

REQUIREMENTS §12 forbids `--no-sandbox`. Crawl4AI 0.9.4's default config includes `--no-sandbox`, and it can be disabled with `CRAWL4AI_CHROMIUM_SANDBOX=true`. Chromium's namespace sandbox then needs user namespaces, which Docker's default seccomp/AppArmor profiles may restrict. The host kernel allows unprivileged user namespaces (`user.max_user_namespaces=63651`, `kernel.unprivileged_userns_clone=1`, checked 2026-10-04).

## Decision

Run Crawl4AI with the sandbox enabled. Build step 3 (Task 3.0) verifies it under Docker's default profiles. If that fails, ship a minimal per-service seccomp profile (never `privileged`, never `SYS_ADMIN`). If that also fails, stop and ask the owner. `scripts/check_sandbox.sh` is the evidence command.

## Consequences

Stronger isolation for untrusted pages. A custom seccomp profile, if one is needed, must be maintained alongside Crawl4AI upgrades.

## Outcome

**2026-10-04, build step 3, Task 3.0: path B (custom seccomp profile) plus a small add-on.** The owner approved this after the spike showed that no configuration-only route exists.

### Root cause

Two independent problems stood in the way:

1. **Docker's default seccomp profile blocks the namespace sandbox.** `clone` with namespace flags, `unshare` and `setns` are allowed only with `CAP_SYS_ADMIN`. A Chromium started by hand without `--no-sandbox` aborts with `FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] No usable sandbox!`. AppArmor `docker-default` does **not** block it.
2. **Crawl4AI 0.9.4 injects `--no-sandbox` whatever `CRAWL4AI_CHROMIUM_SANDBOX` says.** The env var only filters `/app/config.yml:108` on one path (`/app/server.py:117-123`, used at `server.py:135`). The flag is still injected in three places:
   - `crawl4ai/browser_manager.py:1092`: `BrowserManager._build_browser_args()` hardcodes it. `ManagedBrowser.build_browser_flags()` does the same at `:73`.
   - Playwright adds it whenever `chromium_sandbox` is not `True` (driver `coreBundle.js`: `if (options.chromiumSandbox !== true) chromeArguments.push("--no-sandbox")`). Crawl4AI never passes that option (`browser_manager.py:895`, `:967`).
   - The raw config `extra_args` list, still holding the flag, reaches `BrowserConfig` through `/app/api.py:154`, `api.py:262`, `api.py:386` and `/app/monitor_routes.py:282`. `ManagedBrowser.start()` appends it at `browser_manager.py:198`.

### What shipped

- `deploy/crawl4ai/seccomp-chromium.json`: moby's default profile plus one rule allowing `clone`, `unshare` and `setns`. The base commit and how to regenerate it are in `deploy/crawl4ai/README.md`.
- `deploy/crawl4ai/sitecustomize.py`: mounted read-only into the image's `site-packages`, and active only when `CRAWL4AI_CHROMIUM_SANDBOX=true`. It wraps Playwright's `BrowserType.launch` and `launch_persistent_context` to drop the flag and force `chromium_sandbox=True`. It also strips the flag from `ManagedBrowser.build_browser_flags()` and from `extra_args` in `ManagedBrowser.start()`. If a target is missing it warns on stderr and never crashes. All three launch modes were verified sandboxed: default, managed and persistent context.
- **No cleaned `config.yml`.** The add-on filters the final argument list on every launch path, so a copied vendor config would only add drift.
- `compose.yaml` `crawl4ai`: `security_opt: [seccomp=./deploy/crawl4ai/seccomp-chromium.json, no-new-privileges:true]` and the read-only mount. There is no `privileged`, no added capabilities and nothing unconfined. The sandbox works with `no-new-privileges`, because Chromium's namespace sandbox doesn't use setuid.

### Evidence

`scripts/check_sandbox.sh`, before (committed config, env var set, default seccomp):
```
{"sandbox":"off","no_sandbox_procs":5,"crawl_ok":true}
```
After (shipped config):
```
{"sandbox":"on","no_sandbox_procs":0,"renderers":1,"renderer_seccomp_userns":true,"crawl_ok":true}
```
Process evidence during a crawl (`/proc/<pid>/status`, `readlink /proc/<pid>/ns/*`):
```
browser            nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:1 user:[4026531837] pid:[4026532285] net:[4026532287]
--type=zygote      nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:1 user:[4026532534] pid:[4026532475] net:[4026532476]
--type=broker      nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:2 user:[4026531837] ...
--type=renderer    nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:2 user:[4026532534] pid:[4026532351] net:[4026532476]
```
The renderer runs in its own user, PID and net namespaces. Chromium's seccomp-bpf filter is stacked on Docker's (`Seccomp_filters: 2` against the browser's `1`). `Seccomp: 2` alone proves nothing here, because Docker's filter puts it on every process. That is why the check script compares filter counts and user namespaces.

### Removal

Delete `deploy/crawl4ai/sitecustomize.py` and its compose mount once upstream honours `CRAWL4AI_CHROMIUM_SANDBOX` on every launch path. The upstream report is drafted in [`docs/upstream/crawl4ai-chromium-sandbox-issue.md`](../upstream/crawl4ai-chromium-sandbox-issue.md). The seccomp profile stays for as long as Docker's default profile needs `CAP_SYS_ADMIN` for namespace creation.

### Safety net

`scripts/check_sandbox.sh` exits non-zero unless the sandbox is on and the crawl succeeds. It already runs as `tests/integration/test_sandbox_live.py`. In step 9 it becomes part of `make smoke`, and so of `make deploy`. A Crawl4AI upgrade that breaks the add-on, or a Python-version change that moves `site-packages`, fails that check instead of silently disabling the sandbox. `tests/test_sandbox_addon.py` pins the add-on's patch-and-warn behaviour.

### Deferred (V2)

- Block the `crawl4ai` container's egress to the LAN (private ranges) at the network layer, as defence in depth behind the sandbox and the SSRF guard.
- `cap_drop: [ALL]` (step 8): a throwaway test on 2026-10-04 left the container unhealthy before any browser launched. That was the image's entrypoint or supervisord, not Chromium. Step 8 must add back the named capabilities the entrypoint needs and re-run `scripts/check_sandbox.sh`. The namespace sandbox itself needs no capabilities, because it runs as uid 999.
