"""`research-engine` command line: schema export and the smoke test."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from research_engine_client.schemas import render_schemas


def _export(out_dir: Path, check: bool) -> int:
    rendered = render_schemas()
    stale = [
        n
        for n, t in rendered.items()
        if not (out_dir / f"{n}.json").exists() or (out_dir / f"{n}.json").read_text() != t
    ]
    extra = (
        sorted(p.stem for p in out_dir.glob("*.json") if p.stem not in rendered)
        if out_dir.exists()
        else []
    )
    if check:
        if stale or extra:
            print("stale schemas:", ", ".join(sorted(stale + extra)), file=sys.stderr)
            return 1
        print("schemas up to date")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in extra:
        (out_dir / f"{name}.json").unlink()
    for name, text in rendered.items():
        (out_dir / f"{name}.json").write_text(text)
    print(f"wrote {len(rendered)} schemas to {out_dir}")
    return 0


MIN_SCRUB_KEY_LEN = 8
"""Shorter keys are not scrubbed from smoke output by substring (they'd corrupt it)."""


def _smoke(url: str, timeout_s: float) -> int:
    """Print exactly one JSON line; exit 0 if every check passed, 1 if not, 2 on bad setup.

    ``API_KEY`` and ``SITE_HOST`` come from the environment (the app container's own), so the
    key never appears on a command line. ``DEMOS_FILE`` defaults to ``config/demos.yaml``.
    """
    import httpx
    from research_engine_client import ResearchEngineClient

    from research_engine.config_files import load_demos
    from research_engine.smoke import run_smoke

    def emit(result: dict[str, Any]) -> None:
        line = json.dumps({"command": "smoke", **result}, sort_keys=True)
        # Scrub only a real-length key: replacing a short one (say "x") would corrupt the line.
        if len(api_key) >= MIN_SCRUB_KEY_LEN:
            line = line.replace(api_key, "***")
        print(line, flush=True)

    api_key = os.environ.get("API_KEY", "")
    site_host = os.environ.get("SITE_HOST", "")
    missing = [n for n, v in (("API_KEY", api_key), ("SITE_HOST", site_host)) if not v]
    if missing:
        emit({"ok": False, "error": f"missing environment: {', '.join(missing)}"})
        return 2
    try:
        demos = load_demos(os.environ.get("DEMOS_FILE") or "config/demos.yaml")
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)[:500]})
        return 2

    async def run() -> dict[str, Any]:
        async with (
            ResearchEngineClient(url, api_key, timeout_s=timeout_s) as client,
            httpx.AsyncClient(
                base_url=url.rstrip("/"), headers={"X-API-Key": api_key}, timeout=timeout_s
            ) as mcp_http,
        ):
            return await run_smoke(client, demos, timeout_s, mcp_http=mcp_http, site_host=site_host)

    try:
        result = asyncio.run(run())
    except Exception as exc:  # still exactly one JSON line; the type only (no message, no key)
        emit({"ok": False, "error": f"unexpected error: {type(exc).__name__}"})
        return 1
    emit(result)
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    schemas = sub.add_parser("schemas", help="JSON Schema tools")
    schemas_sub = schemas.add_subparsers(dest="action", required=True)
    export = schemas_sub.add_parser("export", help="write schemas/*.json")
    export.add_argument("--out", type=Path, default=Path("schemas"))
    export.add_argument("--check", action="store_true", help="fail if committed schemas are stale")
    smoke = sub.add_parser("smoke", help="run the demo set against a running API (one JSON line)")
    smoke.add_argument("--url", default="http://127.0.0.1:8000", help="API base URL")
    smoke.add_argument(
        "--timeout", type=float, default=110.0, help="overall deadline in seconds (default 110)"
    )
    args = parser.parse_args(argv)
    if args.cmd == "smoke":
        return _smoke(args.url, args.timeout)
    if args.cmd == "schemas" and args.action == "export":
        return _export(args.out, args.check)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
