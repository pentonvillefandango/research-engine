"""ADR-0022 safety net: Crawl4AI on the live stack renders with Chromium's sandbox ON."""

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_sandbox.sh"


def test_crawl4ai_chromium_sandbox_is_on() -> None:
    r = subprocess.run(  # noqa: S603
        [str(SCRIPT)], capture_output=True, text=True, timeout=400, check=False
    )
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1, (r.stdout, r.stderr)
    result = json.loads(lines[0])
    assert r.returncode == 0, (result, r.stderr)
    assert result["sandbox"] == "on", result
    assert result["no_sandbox_procs"] == 0, result
    assert result["default_ok"] is True, result
    assert result["builtin_ok"] is True, result
    assert result["addon_active"] is True, result
    assert result["addon_warnings"] == 0, result
    assert result["crawl_ok"] is True, result
