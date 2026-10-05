"""Unit tests for deploy/crawl4ai/addon/sitecustomize.py (ADR-0022) against stub modules.

Each case runs in a fresh `python -S` subprocess so the import hook never touches this
process. The live behaviour is covered by tests/integration/test_sandbox_live.py.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parents[1] / "deploy" / "crawl4ai" / "addon"
ACTIVE = "[research-engine sandbox add-on] active"
WARN = "[research-engine sandbox add-on] WARNING"

PLAYWRIGHT_STUB = """
class BrowserType:
    async def launch(self, **kw):
        return kw

    async def launch_persistent_context(self, user_data_dir, **kw):
        return kw
"""

BROWSER_MANAGER_STUB = """
import subprocess

class ManagedBrowser:
    def __init__(self, cfg):
        self.browser_config = cfg

    @staticmethod
    def build_browser_flags(config, *extra):
        return ["--no-sandbox", "--disable-gpu", *extra]

    async def start(self):
        return list(self.browser_config.extra_args)

    def popen(self, argv):
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        return p.communicate()[0].split()
"""

DRIVER = """
import asyncio, os, sys, types
import sitecustomize  # noqa: F401  (python -S: import explicitly)
import playwright.async_api as pw
import crawl4ai.browser_manager as bm

async def main():
    kw = await pw.BrowserType().launch(args=["--no-sandbox", "--disable-gpu"])
    print("launch", kw["args"], kw.get("chromium_sandbox"))
    kw = await pw.BrowserType().launch_persistent_context("/tmp/x", args=["--no-sandbox"])
    print("persistent", kw["args"], kw.get("chromium_sandbox"))
    print("flags", bm.ManagedBrowser.build_browser_flags(None))
    print("flags-extra", bm.ManagedBrowser.build_browser_flags(None, "--y"))
    cfg = types.SimpleNamespace(extra_args=["--no-sandbox", "--x"])
    print("managed", await bm.ManagedBrowser(cfg).start())
    chrome = os.environ["FAKE_CHROME"]
    print("popen-chrome", bm.ManagedBrowser(cfg).popen([chrome, "--no-sandbox", "--z"]))
    print("popen-other", bm.ManagedBrowser(cfg).popen(["/bin/echo", "--no-sandbox"]))
    real_popen = __import__("subprocess").Popen
    print("popen-isinstance", isinstance(bm.subprocess.Popen(["/bin/true"]), real_popen))
    if "patchright" in sys.modules or os.environ.get("WITH_PATCHRIGHT"):
        import patchright.async_api as pr
        kw = await pr.BrowserType().launch(args=["--no-sandbox"])
        print("patchright", kw["args"], kw.get("chromium_sandbox"))

asyncio.run(main())
"""


def _run(
    tmp_path: Path,
    sandbox: str = "true",
    playwright: str = PLAYWRIGHT_STUB,
    browser_manager: str = BROWSER_MANAGER_STUB,
    *,
    with_crawl4ai: bool = True,
    with_patchright: bool = False,
    code: str = DRIVER,
) -> subprocess.CompletedProcess[str]:
    pkgs = {"playwright": ("async_api.py", playwright)}
    if with_crawl4ai:
        pkgs["crawl4ai"] = ("browser_manager.py", browser_manager)
    if with_patchright:
        pkgs["patchright"] = ("async_api.py", playwright)
    for pkg, (mod, body) in pkgs.items():
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
        (tmp_path / pkg / mod).write_text(textwrap.dedent(body))
    chrome = tmp_path / "fake-chrome-headless-shell"
    chrome.write_text('#!/bin/sh\necho "$@"\n')
    chrome.chmod(0o755)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": f"{tmp_path}:{ADDON_DIR}",
        "CRAWL4AI_CHROMIUM_SANDBOX": sandbox,
        "FAKE_CHROME": str(chrome),
    }
    if with_patchright:
        env["WITH_PATCHRIGHT"] = "1"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", code], env=env, capture_output=True, text=True, check=False
    )


def test_patches_every_launch_path(tmp_path: Path) -> None:
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "launch ['--disable-gpu'] True" in r.stdout
    assert "persistent [] True" in r.stdout
    assert "flags ['--disable-gpu']" in r.stdout
    assert "flags-extra ['--disable-gpu', '--y']" in r.stdout
    assert "managed ['--x']" in r.stdout
    assert WARN not in r.stderr


def test_popen_shim_strips_flag_for_chromium_only(tmp_path: Path) -> None:
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "popen-chrome ['--z']" in r.stdout
    assert "popen-other ['--no-sandbox']" in r.stdout
    assert "popen-isinstance True" in r.stdout


def test_patchright_is_hooked(tmp_path: Path) -> None:
    r = _run(tmp_path, with_patchright=True)
    assert r.returncode == 0, r.stderr
    assert "patchright [] True" in r.stdout


def test_prints_active_line_once_when_installed(tmp_path: Path) -> None:
    r = _run(tmp_path)
    assert r.stderr.count(ACTIVE) == 1, r.stderr


def test_missing_targets_warn_loudly_and_do_not_crash(tmp_path: Path) -> None:
    code = "import sitecustomize, playwright.async_api, crawl4ai.browser_manager; print('ok')"
    r = _run(
        tmp_path,
        playwright="class BrowserType:\n    pass\n",
        browser_manager="class ManagedBrowser:\n    pass\n",
        code=code,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    assert "BrowserType.launch not found" in r.stderr
    assert "BrowserType.launch_persistent_context not found" in r.stderr
    assert "ManagedBrowser.build_browser_flags" in r.stderr
    assert "subprocess" in r.stderr  # no module-level subprocess to shim
    assert "may run WITHOUT its sandbox" in r.stderr


def test_unexpected_wrapper_shapes_warn_and_do_not_crash(tmp_path: Path) -> None:
    odd = """
    import subprocess

    class ManagedBrowser:
        build_browser_flags = 5          # not callable
        start = "nope"
    """
    code = (
        "import sitecustomize, crawl4ai.browser_manager as bm; "
        "print(bm.ManagedBrowser.build_browser_flags)"
    )
    r = _run(tmp_path, browser_manager=odd, code=code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "5"
    assert WARN in r.stderr


def test_instance_method_flags_keep_binding(tmp_path: Path) -> None:
    bm = """
    import subprocess

    class ManagedBrowser:
        def build_browser_flags(self, config):
            return ["--no-sandbox", self.tag]

        async def start(self):
            return None
    """
    code = textwrap.dedent("""
        import sitecustomize, crawl4ai.browser_manager as bm
        m = bm.ManagedBrowser(); m.tag = "--t"
        print(m.build_browser_flags(None))
    """)
    r = _run(tmp_path, browser_manager=bm, code=code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['--t']"
    assert WARN not in r.stderr


def test_inert_when_flag_off(tmp_path: Path) -> None:
    r = _run(tmp_path, sandbox="false")
    assert r.returncode == 0, r.stderr
    assert "launch ['--no-sandbox', '--disable-gpu'] None" in r.stdout
    assert "flags ['--no-sandbox', '--disable-gpu']" in r.stdout
    assert ACTIVE not in r.stderr


def test_inert_when_crawl4ai_not_installed(tmp_path: Path) -> None:
    """supervisord's Debian Python also sees PYTHONPATH; it has no crawl4ai."""
    r = _run(tmp_path, with_crawl4ai=False, code="import sitecustomize, sys; print('ok')")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    assert r.stderr == ""
