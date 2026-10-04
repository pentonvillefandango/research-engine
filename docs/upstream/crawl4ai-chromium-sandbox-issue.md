Status: draft, not yet filed

# `CRAWL4AI_CHROMIUM_SANDBOX=true` does not enable Chromium's sandbox: `--no-sandbox` is still injected by `BrowserManager` and by Playwright

## Environment

- Image: `unclecode/crawl4ai:0.9.4` (Python 3.12, bundled Playwright with `chromium_headless_shell-1243`)
- Docker Engine 29.x, default AppArmor profile, on a Linux host that allows unprivileged user namespaces.

## Expected

`server.py` documents `CRAWL4AI_CHROMIUM_SANDBOX=true` as the way to "drop --no-sandbox and run the renderer sandboxed". With it set, and with the host and container able to sandbox, no Chromium process should carry `--no-sandbox`. Renderers should then run in Chromium's namespace and seccomp-bpf sandbox.

## Actual

Every Chromium process launched by `/crawl` still has `--no-sandbox`. On the browser process it appears twice:

```
chrome-headless-shell ... --headless --no-sandbox --no-sandbox ...
chrome-headless-shell --type=renderer ... --no-sandbox ...
```

The env var removes the flag from only one of several places that inject it.

## Where `--no-sandbox` comes from (0.9.4)

1. **`crawl4ai/browser_manager.py:1092`**: `BrowserManager._build_browser_args()` hardcodes `"--no-sandbox"` in its base list. This is the Playwright `chromium.launch()` path, at `:967`. `ManagedBrowser.build_browser_flags()` also hardcodes it, at **`browser_manager.py:73`**. That covers the managed/CDP path and the `use_persistent_context` path (`:862`). `ManagedBrowser` starts Chromium itself with `subprocess.Popen` (`:251`/`:258`). On the Docker server, a request body reaches that path with `"browser_mode": "builtin"`, which is on `UNTRUSTED_FIELD_ALLOWLIST` (`async_configs.py:247`).
2. **Playwright**: `chromium.launch()` and `launch_persistent_context()` append `--no-sandbox` whenever `chromium_sandbox` is not `True` (in the driver: `if (options.chromiumSandbox !== true) chromeArguments.push("--no-sandbox")`). Crawl4AI never passes `chromium_sandbox`, at `browser_manager.py:895` or `:967`. Patchright, used when `use_undetected` is set (`:627`/`:713`/`:833`), has the same default.
3. **Docker server config**: `config.yml:108` (`/app/config.yml` in the image) lists `--no-sandbox` in `crawler.browser.extra_args`. `server.py:117-123` `_browser_extra_args()` strips it when the env var is set, but it is used only by `get_default_browser_config()` (`server.py:135`). These call sites pass the raw list instead: **`api.py:154`, `api.py:262`, `api.py:386`** and **`monitor_routes.py:282`**, all `extra_args=cfg["crawler"]["browser"].get("extra_args", [])`. `ManagedBrowser.start()` appends `extra_args` after its own flags (`browser_manager.py:198`).

## Minimal reproduction

```bash
docker run -d --name c4ai -e CRAWL4AI_API_TOKEN=test -e CRAWL4AI_CHROMIUM_SANDBOX=true \
  --shm-size=1g --security-opt seccomp=./seccomp-chromium.json unclecode/crawl4ai:0.9.4
# (seccomp-chromium.json = Docker's default profile + the rule in "Note on Docker" below)
sleep 40
docker exec c4ai curl -s -H 'Authorization: Bearer test' -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"]}' http://localhost:11235/crawl >/dev/null &
sleep 3
docker exec c4ai sh -c 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f"; echo; done' \
  | grep chrome-headless-shell | grep -c -- --no-sandbox     # expected 0, actual > 0
```

## Suggested fix

When `CRAWL4AI_CHROMIUM_SANDBOX` is true, or better when a `BrowserConfig` field such as `chromium_sandbox: bool` is true:

- pass `chromium_sandbox=True` to Playwright's and Patchright's `launch()` and `launch_persistent_context()`;
- don't add `--no-sandbox` in `BrowserManager._build_browser_args()` or `ManagedBrowser.build_browser_flags()`, including the `ManagedBrowser` `Popen` launch used by `browser_mode="builtin"`;
- strip `--no-sandbox` from `extra_args` on **every** config path: `api.py`, `monitor_routes.py` and `ManagedBrowser.start()`. One shared helper, like `server._browser_extra_args()`, used everywhere, would do it.

We have confirmed that with these changes, sandboxed crawls succeed in the container as non-root, on both the default and `builtin` paths, and that this holds with `no-new-privileges:true`. The renderers run in their own user, PID and net namespaces, with Chromium's seccomp-bpf filter stacked on Docker's.

## Note on Docker's default seccomp profile

Even with the flag removed, Chromium aborts under Docker's **default** seccomp profile with `FATAL ... No usable sandbox!`. In that profile, `clone` with namespace flags and `unshare` are allowed only with `CAP_SYS_ADMIN`. A profile that equals the default plus one rule is enough. It needs no extra capabilities, no privileged mode and no unconfined profile:

```json
{"names": ["clone", "unshare"], "action": "SCMP_ACT_ALLOW"}
```

We tested both directions: `setns` is not needed, and `clone` alone is not enough.

Note that this rule lets any process in the container create user and net namespaces. Renderers can't, because of Chromium's own seccomp-bpf. That widens the kernel attack surface, for example `nf_tables`, so the host kernel should be kept patched.

It would help to document this next to `CRAWL4AI_CHROMIUM_SANDBOX`, and perhaps ship such a profile.
