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

1. **Docker's default seccomp profile blocks the namespace sandbox.** `clone` with namespace flags and `unshare` are allowed only with `CAP_SYS_ADMIN`. A Chromium started by hand without `--no-sandbox` aborts with `FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] No usable sandbox!`. AppArmor `docker-default` does **not** block it.
2. **Crawl4AI 0.9.4 injects `--no-sandbox` whatever `CRAWL4AI_CHROMIUM_SANDBOX` says.** The env var only filters `/app/config.yml:108` on one path (`/app/server.py:117-123`, used at `server.py:135`). The flag is still injected in three places:
   - `crawl4ai/browser_manager.py:1092`: `BrowserManager._build_browser_args()` hardcodes it. `ManagedBrowser.build_browser_flags()` does the same at `:73`.
   - Playwright adds it whenever `chromium_sandbox` is not `True` (driver `coreBundle.js`: `if (options.chromiumSandbox !== true) chromeArguments.push("--no-sandbox")`). Crawl4AI never passes that option (`browser_manager.py:895`, `:967`).
   - The raw config `extra_args` list, still holding the flag, reaches `BrowserConfig` through `/app/api.py:154`, `api.py:262`, `api.py:386` and `/app/monitor_routes.py:282`. `ManagedBrowser.start()` appends it at `browser_manager.py:198`, then launches Chromium itself with `subprocess.Popen` (`:251`/`:258`). Request bodies reach that path with `browser_mode="builtin"`, which is on Crawl4AI's untrusted-field allowlist (`async_configs.py:247`).
   - Patchright (used when `use_undetected` is set, `browser_manager.py:627`/`713`/`833`) behaves like Playwright.

### What shipped

- `deploy/crawl4ai/seccomp-chromium.json`: moby's default profile plus one rule allowing `clone` and `unshare`. The base commit and how to regenerate it are in `deploy/crawl4ai/README.md`. Tested on 2026-10-04:
  - `setns` (in the first version) is **not** needed: `scripts/check_sandbox.sh` stayed green on both launch paths without it, so it was removed.
  - `clone` alone is not enough: Chromium aborts with `No usable sandbox!`.
- `deploy/crawl4ai/addon/sitecustomize.py`: mounted read-only at `/opt/re-addon`, loaded through `PYTHONPATH`, so it doesn't depend on the Python version. It installs only when `CRAWL4AI_CHROMIUM_SANDBOX=true` and `crawl4ai` is importable, which keeps it inert in supervisord's Debian Python. On install it prints one `[research-engine sandbox add-on] active (...)` line. It patches:
  - `playwright.async_api` and `patchright.async_api`: `BrowserType.launch` and `launch_persistent_context` drop the flag and force `chromium_sandbox=True`;
  - `crawl4ai.browser_manager`: the module's `subprocess` is replaced by a shim whose `Popen` strips the flag from any argv whose argv0 contains `chrom`. This is the choke point for the builtin path. A throwaway variant with only the shim kept builtin launches sandboxed. Wrappers on `ManagedBrowser.build_browser_flags()` (binding-preserving, via `inspect.getattr_static`) and `ManagedBrowser.start()` remain as extra protection.

  Any missing or odd-shaped target prints `[research-engine sandbox add-on] WARNING: ...` to stderr. It never crashes Crawl4AI.
- **No cleaned `config.yml`.** The add-on filters the final argument list on every launch path, so a copied vendor config would only add drift.
- `compose.yaml` `crawl4ai`: `security_opt: [seccomp=./deploy/crawl4ai/seccomp-chromium.json, no-new-privileges:true]`, `PYTHONPATH: /opt/re-addon` and the read-only `./deploy/crawl4ai/addon` mount. There is no `privileged`, no added capabilities and nothing unconfined. The sandbox works with `no-new-privileges`, because Chromium's namespace sandbox doesn't use setuid.

### Evidence

`scripts/check_sandbox.sh`, before (committed config, env var set, default seccomp):
```
{"sandbox":"off","no_sandbox_procs":5,"crawl_ok":true}
```
After (shipped config, check script covering both launch paths):
```
{"sandbox":"on","no_sandbox_procs":0,"default_ok":true,"default_new_renderers":1,"builtin_ok":true,"builtin_new_renderers":7,"addon_active":true,"addon_warnings":0,"crawl_ok":true}
```
Negative controls, each a throwaway variant on 2026-10-04 that the check correctly failed:

| Variant | Result |
|---|---|
| Add-on not loaded | `addon_active:false`, `no_sandbox_procs:35` |
| `crawl4ai.browser_manager` not hooked | `builtin_ok:false`, `no_sandbox_procs:13` |
| Add-on emits a WARNING | `addon_warnings:1` |
| `clone`-only seccomp rule | `crawl_ok:false` (`No usable sandbox!`) |
Process evidence during a crawl (`/proc/<pid>/status`, `readlink /proc/<pid>/ns/*`):
```
browser            nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:1 user:[4026531837] pid:[4026532285] net:[4026532287]
--type=zygote      nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:1 user:[4026532534] pid:[4026532475] net:[4026532476]
--type=broker      nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:2 user:[4026531837] ...
--type=renderer    nosandbox=0 NoNewPrivs:1 Seccomp:2 Seccomp_filters:2 user:[4026532534] pid:[4026532351] net:[4026532476]
```
The renderer runs in its own user, PID and net namespaces. Chromium's seccomp-bpf filter is stacked on Docker's (`Seccomp_filters: 2` against the browser's `1`). `Seccomp: 2` alone proves nothing here, because Docker's filter puts it on every process. That is why the check script compares filter counts and user namespaces, each renderer against **its own** parent browser. It counts only renderers that are **new** during the crawl and belong to the browser kind of the path under test: `--remote-debugging-pipe` for Playwright, `--remote-debugging-port=` for `ManagedBrowser`. A per-run `viewport_width` forces a fresh launch on each path.

### Residual risk

The seccomp rule applies to **every process in the container**, not just Chromium's sandbox setup. The gunicorn server, the Playwright node driver, the Chromium browser process and any shell can now `unshare(CLONE_NEWUSER|CLONE_NEWNET)`. This was verified: `unshare -Urn` succeeds as uid 999. Inside such a namespace a process holds capabilities over its own namespaces. That exposes kernel code that is otherwise gated behind `CAP_NET_ADMIN`, chiefly netfilter/`nf_tables`, the CVE-2024-1086 class of local privilege escalations.

Limits on that exposure:
- Renderers, which run the untrusted page content, can't create namespaces. Chromium's own seccomp-bpf denies it.
- `mount`, `bpf` and `perf_event_open` stay blocked, because the default profile still reserves them for `CAP_SYS_ADMIN`. All three were verified `EPERM`.
- Every process runs as uid 999 with `CapEff=0`. The only non-zero value is Chromium's sandboxed zygote, and that applies inside its own new user namespace.

This still beats `--no-sandbox`. Without the sandbox, a renderer exploit runs straight away as the container user, with the network service and the whole container filesystem in reach, and can use these same namespace syscalls. With the sandbox, an attacker needs a renderer exploit **and** a sandbox escape before reaching the extra kernel surface.

Compensating controls:
- **Keep the host kernel patched.** This is the primary control for the nf_tables class.
- **Optionally stop the host autoloading `nf_tables`** if the host doesn't need it, for example with `install nf_tables /bin/false` in modprobe.d. That needs the owner, because it requires root on the host.
- **A future upstream fix doesn't remove this.** Even if upstream fixes the flag, the seccomp profile stays for as long as Docker's default profile reserves namespace creation for `CAP_SYS_ADMIN`.

### Removal

Delete `deploy/crawl4ai/addon/`, its compose mount and `PYTHONPATH` once upstream honours `CRAWL4AI_CHROMIUM_SANDBOX` on every launch path. The upstream report is drafted in [`docs/upstream/crawl4ai-chromium-sandbox-issue.md`](../upstream/crawl4ai-chromium-sandbox-issue.md). The seccomp profile stays for as long as Docker's default profile needs `CAP_SYS_ADMIN` for namespace creation.

### Safety net

`scripts/check_sandbox.sh` exits non-zero unless all of these hold:
- both launch paths (default and `browser_mode=builtin`) crawl successfully with a new, sandboxed renderer;
- no Chromium process carries `--no-sandbox`;
- the add-on's `active` line is in the container logs since start;
- no add-on `WARNING` is.

It already runs as `tests/integration/test_sandbox_live.py`. In step 9 it becomes part of `make smoke`, and so of `make deploy`. A Crawl4AI upgrade that breaks the add-on fails that check instead of silently disabling the sandbox. `tests/test_sandbox_addon.py` pins the add-on's behaviour: the Popen shim, binding-safe wrappers, the patchright target, warn-not-crash, the `active` line, and staying inert without the flag or without crawl4ai.

### Deferred (V2)

- Block the `crawl4ai` container's egress to the LAN (private ranges) at the network layer, as defence in depth behind the sandbox and the SSRF guard.
- `cap_drop: [ALL]` (step 8): a throwaway test on 2026-10-04 left the container unhealthy, and no browser launched. The cause is unknown; every process already runs as uid 999 with `CapEff=0`. Step 8 inspects the container logs and re-runs `scripts/check_sandbox.sh`.
