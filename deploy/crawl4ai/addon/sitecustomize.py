"""research-engine add-on: keep Chromium's sandbox ON in unclecode/crawl4ai:0.9.4 (ADR-0022).

WHY: CRAWL4AI_CHROMIUM_SANDBOX=true only strips the sandbox-disabling flag from the
config.yml list on one code path (/app/server.py:117-123). The flag is still injected by:
  1. crawl4ai/browser_manager.py:1092  BrowserManager._build_browser_args() hardcodes it;
  2. crawl4ai/browser_manager.py:73    ManagedBrowser.build_browser_flags() hardcodes it, and
     ManagedBrowser.start() (:198) appends config extra_args that still carry it, because
     /app/api.py:154/262/386 and /app/monitor_routes.py:282 pass the raw config list.
     ManagedBrowser launches Chromium itself via subprocess.Popen (:251/:258); request bodies
     reach it with browser_mode="builtin" (async_configs.py:247 allowlist);
  3. Playwright (and Patchright, used when use_undetected is set: browser_manager.py:627/713/833)
     add it whenever launch(chromium_sandbox=...) is not True; crawl4ai never passes it
     (:895, :967).

WHAT: only when CRAWL4AI_CHROMIUM_SANDBOX=true and crawl4ai is importable (so supervisord's
Debian Python, which also sees PYTHONPATH, stays inert), lazily on import of each target:
  - {playwright,patchright}.async_api.BrowserType.launch / launch_persistent_context: drop the
    flag from `args` and force chromium_sandbox=True (covers 1 and 3);
  - crawl4ai.browser_manager: its module-level `subprocess` becomes a shim whose Popen strips the
    flag from any argv whose argv0 contains "chrom" (choke point for 2), plus belt-and-braces
    wrappers on ManagedBrowser.build_browser_flags() and ManagedBrowser.start().
On install it prints one "active" line to stderr; scripts/check_sandbox.sh requires it. If a
target is missing or has an unexpected shape, Crawl4AI keeps running and a WARNING goes to
stderr; check_sandbox.sh fails on that WARNING. It never fails silently.

DELETE THIS DIRECTORY (and its compose mount and PYTHONPATH) once upstream honours
CRAWL4AI_CHROMIUM_SANDBOX on every launch path; see
docs/upstream/crawl4ai-chromium-sandbox-issue.md.
"""

import functools
import importlib.abc
import importlib.util
import inspect
import os
import subprocess as _subprocess
import sys
import types

_FLAG = "--no-sandbox"
_TAG = "[research-engine sandbox add-on]"


def _warn(msg: str) -> None:
    sys.stderr.write(f"{_TAG} WARNING: {msg}; Chromium may run WITHOUT its sandbox\n")


def _strip(args):
    return [a for a in args if a != _FLAG] if isinstance(args, list | tuple) else args


def _patch_browser_type(mod) -> None:
    bt = getattr(mod, "BrowserType", None)
    for name in ("launch", "launch_persistent_context"):
        orig = getattr(bt, name, None)
        if not callable(orig):
            _warn(f"{mod.__name__}.BrowserType.{name} not found")
            continue

        def make(orig):
            @functools.wraps(orig)
            async def wrapper(self, *a, **kw):
                kw["args"] = _strip(kw.get("args"))
                kw["chromium_sandbox"] = True
                return await orig(self, *a, **kw)

            return wrapper

        setattr(bt, name, make(orig))


class _Popen(_subprocess.Popen):
    """Popen that removes the sandbox-disabling flag from Chromium command lines."""

    def __init__(self, args, *a, **kw):
        if isinstance(args, list | tuple) and args and "chrom" in os.path.basename(str(args[0])):
            args = _strip(list(args))
        elif isinstance(args, str | bytes) and "chrom" in str(args) and _FLAG in str(args):
            _warn("Chromium launched with a shell string; cannot strip the flag safely")
        super().__init__(args, *a, **kw)


def _subprocess_shim() -> types.ModuleType:
    shim = types.ModuleType("subprocess")
    shim.__dict__.update(_subprocess.__dict__)
    shim.Popen = _Popen  # type: ignore[attr-defined]
    return shim


def _wrap_flags(mb) -> None:
    raw = inspect.getattr_static(mb, "build_browser_flags", None)
    kind = (
        staticmethod
        if isinstance(raw, staticmethod)
        else classmethod
        if isinstance(raw, classmethod)
        else None
    )
    func = raw.__func__ if kind else raw
    if not inspect.isfunction(func):
        _warn(f"ManagedBrowser.build_browser_flags has unexpected shape {type(raw).__name__}")
        return

    @functools.wraps(func)
    def wrapper(*a, **kw):
        return _strip(func(*a, **kw))

    mb.build_browser_flags = kind(wrapper) if kind else wrapper


def _wrap_start(mb) -> None:
    start = inspect.getattr_static(mb, "start", None)
    if not inspect.iscoroutinefunction(start):
        _warn(f"ManagedBrowser.start has unexpected shape {type(start).__name__}")
        return

    @functools.wraps(start)
    async def wrapper(self, *a, **kw):
        cfg = getattr(self, "browser_config", None)
        if cfg is not None and getattr(cfg, "extra_args", None):
            cfg.extra_args = _strip(cfg.extra_args)
        return await start(self, *a, **kw)

    mb.start = wrapper


def _patch_browser_manager(mod) -> None:
    if getattr(mod, "subprocess", None) is _subprocess:
        mod.subprocess = _subprocess_shim()
    else:
        _warn("crawl4ai.browser_manager has no module-level subprocess to shim")
    mb = getattr(mod, "ManagedBrowser", None)
    if mb is None:
        _warn("crawl4ai.browser_manager.ManagedBrowser not found")
        return
    for wrap in (_wrap_flags, _wrap_start):
        try:
            wrap(mb)
        except Exception as exc:
            _warn(f"{wrap.__name__} failed: {exc!r}")


_TARGETS = {
    "playwright.async_api": _patch_browser_type,
    "patchright.async_api": _patch_browser_type,
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


def _install() -> None:
    if os.environ.get("CRAWL4AI_CHROMIUM_SANDBOX", "false").lower() != "true":
        return
    try:
        if importlib.util.find_spec("crawl4ai") is None:
            return  # not the crawl4ai interpreter (e.g. supervisord's system Python)
        sys.meta_path.insert(0, _Finder())
        sys.stderr.write(
            f"{_TAG} active (python {sys.version_info[0]}.{sys.version_info[1]}, "
            f"pid {os.getpid()}, targets: {', '.join(_TARGETS)})\n"
        )
    except Exception as exc:
        _warn(f"could not install import hook: {exc!r}")


_install()
