"""Unit tests for deploy/crawl4ai/sitecustomize.py (ADR-0022) against stub modules.

Each case runs in a fresh `python -S` subprocess so the import hook never touches this
process. The live behaviour is covered by tests/integration/test_sandbox_live.py.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parents[1] / "deploy" / "crawl4ai"

PLAYWRIGHT_STUB = """
class BrowserType:
    async def launch(self, **kw):
        return kw

    async def launch_persistent_context(self, user_data_dir, **kw):
        return kw
"""

BROWSER_MANAGER_STUB = """
class ManagedBrowser:
    def __init__(self, cfg):
        self.browser_config = cfg

    @staticmethod
    def build_browser_flags(config):
        return ["--no-sandbox", "--disable-gpu"]

    async def start(self):
        return list(self.browser_config.extra_args)
"""

DRIVER = """
import asyncio, types
import sitecustomize  # noqa: F401  (python -S: import explicitly)
import playwright.async_api as pw
import crawl4ai.browser_manager as bm

async def main():
    kw = await pw.BrowserType().launch(args=["--no-sandbox", "--disable-gpu"])
    print("launch", kw["args"], kw.get("chromium_sandbox"))
    kw = await pw.BrowserType().launch_persistent_context("/tmp/x", args=["--no-sandbox"])
    print("persistent", kw["args"], kw.get("chromium_sandbox"))
    print("flags", bm.ManagedBrowser.build_browser_flags(None))
    cfg = types.SimpleNamespace(extra_args=["--no-sandbox", "--x"])
    print("managed", await bm.ManagedBrowser(cfg).start())

asyncio.run(main())
"""


def _run(
    tmp_path: Path, sandbox: str, playwright: str, browser_manager: str
) -> subprocess.CompletedProcess[str]:
    for pkg in ("playwright", "crawl4ai"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / "playwright" / "async_api.py").write_text(textwrap.dedent(playwright))
    (tmp_path / "crawl4ai" / "browser_manager.py").write_text(textwrap.dedent(browser_manager))
    env = {
        "PYTHONPATH": f"{tmp_path}:{ADDON_DIR}",
        "CRAWL4AI_CHROMIUM_SANDBOX": sandbox,
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", DRIVER], env=env, capture_output=True, text=True, check=False
    )


def test_patches_every_launch_path(tmp_path: Path) -> None:
    r = _run(tmp_path, "true", PLAYWRIGHT_STUB, BROWSER_MANAGER_STUB)
    assert r.returncode == 0, r.stderr
    assert "launch ['--disable-gpu'] True" in r.stdout
    assert "persistent [] True" in r.stdout
    assert "flags ['--disable-gpu']" in r.stdout
    assert "managed ['--x']" in r.stdout
    assert "WARNING" not in r.stderr


def test_missing_targets_warn_loudly_and_do_not_crash(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "true",
        "class BrowserType:\n    pass\n",
        "class ManagedBrowser:\n    pass\n",
    )
    # the stubs lack the methods the driver calls, so it fails *after* import; the
    # point is that importing crawl4ai/playwright survived and the add-on warned.
    assert "BrowserType.launch not found" in r.stderr
    assert "BrowserType.launch_persistent_context not found" in r.stderr
    assert "ManagedBrowser.build_browser_flags/start not found" in r.stderr
    assert "may run WITHOUT its sandbox" in r.stderr
    assert "AttributeError" in r.stderr  # from the driver's own call, not from the add-on


def test_inert_when_flag_off(tmp_path: Path) -> None:
    r = _run(tmp_path, "false", PLAYWRIGHT_STUB, BROWSER_MANAGER_STUB)
    assert r.returncode == 0, r.stderr
    assert "launch ['--no-sandbox', '--disable-gpu'] None" in r.stdout
    assert "flags ['--no-sandbox', '--disable-gpu']" in r.stdout
