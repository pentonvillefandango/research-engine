"""Offline integrity of the vendored htmx assets (scripts/vendor_assets.sh checks online)."""

import base64
import hashlib
import re

from tests.conftest import ROOT

VENDOR = ROOT / "packages/research_engine/src/research_engine/gui/static/vendor"
SCRIPT = ROOT / "scripts/vendor_assets.sh"


def _vendor_md_rows() -> dict[str, tuple[str, str, str]]:
    rows: dict[str, tuple[str, str, str]] = {}
    for line in (VENDOR / "VENDOR.md").read_text().splitlines():
        m = re.match(
            r"\| `([\w.]+\.js)` \| `([\w.@-]+)` .*`(sha512-[^`]+)` \| `(sha384-[^`]+)` \|", line
        )
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return rows


def test_vendored_files_match_recorded_sri() -> None:
    rows = _vendor_md_rows()
    assert set(rows) == {"htmx.min.js", "sse.js"}
    for name, (_, _, sri) in rows.items():
        digest = base64.b64encode(hashlib.sha384((VENDOR / name).read_bytes()).digest()).decode()
        assert sri == f"sha384-{digest}", name


def test_script_pins_the_recorded_tarball_integrity() -> None:
    script = SCRIPT.read_text()
    pins = dict(re.findall(r"^fetch (\S+ \S+) \S+ \S+ (sha512-\S+)$", script, re.M))
    expected = {
        pkg.replace("@", " "): integrity for pkg, integrity, _ in _vendor_md_rows().values()
    }
    assert pins == expected
