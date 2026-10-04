"""research-engine add-on: keep Chromium's sandbox ON in unclecode/crawl4ai:0.9.4 (ADR-0022).

WHY: CRAWL4AI_CHROMIUM_SANDBOX=true only strips the sandbox-disabling flag from the
config.yml list on one code path (/app/server.py:117-123). The flag is still injected by:
  1. crawl4ai/browser_manager.py:1092  BrowserManager._build_browser_args() hardcodes it;
  2. crawl4ai/browser_manager.py:73    ManagedBrowser.build_browser_flags() hardcodes it
     (and ManagedBrowser.start(), :198, appends config extra_args, which still carry it,
     because /app/api.py:154/262/386 and /app/monitor_routes.py:282 pass the raw list);
  3. Playwright's driver adds it whenever launch(chromium_sandbox=...) is not True, and
     crawl4ai never passes that option (browser_manager.py:895, :967).

WHAT: only when CRAWL4AI_CHROMIUM_SANDBOX=true, and lazily on import of the target module:
  - playwright.async_api.BrowserType.launch / launch_persistent_context: drop the flag from
    `args` and force chromium_sandbox=True (covers 1 and 3);
  - crawl4ai.browser_manager.ManagedBrowser: drop the flag from build_browser_flags() and from
    browser_config.extra_args before start() (covers 2).
If a target is missing (e.g. after an upgrade) Crawl4AI keeps running and a WARNING goes to
stderr; scripts/check_sandbox.sh then reports sandbox "off". It never fails silently.

DELETE THIS FILE (and its compose mount) once upstream honours CRAWL4AI_CHROMIUM_SANDBOX on
every launch path; see docs/upstream/crawl4ai-chromium-sandbox-issue.md.
"""

import functools
import importlib.abc
import os
import sys

_FLAG = "--no-sandbox"
_TAG = "[research-engine sandbox add-on]"


def _warn(msg: str) -> None:
    sys.stderr.write(f"{_TAG} WARNING: {msg}; Chromium may run WITHOUT its sandbox\n")


def _strip(args):
    return [a for a in args if a != _FLAG] if args else args


def _patch_playwright(mod) -> None:
    bt = getattr(mod, "BrowserType", None)
    for name in ("launch", "launch_persistent_context"):
        orig = getattr(bt, name, None)
        if orig is None:
            _warn(f"playwright.async_api.BrowserType.{name} not found")
            continue

        def make(orig):
            @functools.wraps(orig)
            async def wrapper(self, *a, **kw):
                kw["args"] = _strip(kw.get("args"))
                kw["chromium_sandbox"] = True
                return await orig(self, *a, **kw)

            return wrapper

        setattr(bt, name, make(orig))


def _patch_browser_manager(mod) -> None:
    mb = getattr(mod, "ManagedBrowser", None)
    flags = getattr(mb, "build_browser_flags", None)
    start = getattr(mb, "start", None)
    if flags is None or start is None:
        _warn("crawl4ai.browser_manager.ManagedBrowser.build_browser_flags/start not found")
        return
    mb.build_browser_flags = staticmethod(functools.wraps(flags)(lambda c: _strip(flags(c))))

    @functools.wraps(start)
    async def start_wrapper(self, *a, **kw):
        cfg = getattr(self, "browser_config", None)
        if cfg is not None and getattr(cfg, "extra_args", None):
            cfg.extra_args = _strip(cfg.extra_args)
        return await start(self, *a, **kw)

    mb.start = start_wrapper


_TARGETS = {
    "playwright.async_api": _patch_playwright,
    "crawl4ai.browser_manager": _patch_browser_manager,
}


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        patch = _TARGETS.get(name)
        if patch is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        exec_module = getattr(loader, "exec_module", None)
        if exec_module is None:
            _warn(f"cannot hook import of {name}")
            return spec

        def hooked(module):
            exec_module(module)
            try:
                patch(module)
            except Exception as exc:  # never break Crawl4AI; check_sandbox.sh catches it
                _warn(f"patching {name} failed: {exc!r}")

        loader.exec_module = hooked  # type: ignore[method-assign]
        return spec


if os.environ.get("CRAWL4AI_CHROMIUM_SANDBOX", "false").lower() == "true":
    try:
        sys.meta_path.insert(0, _Finder())
    except Exception as exc:
        _warn(f"could not install import hook: {exc!r}")
