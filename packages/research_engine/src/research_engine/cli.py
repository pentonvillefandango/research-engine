"""`research-engine` command line: schema export now; more subcommands later."""

import argparse
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    schemas = sub.add_parser("schemas", help="JSON Schema tools")
    schemas_sub = schemas.add_subparsers(dest="action", required=True)
    export = schemas_sub.add_parser("export", help="write schemas/*.json")
    export.add_argument("--out", type=Path, default=Path("schemas"))
    export.add_argument("--check", action="store_true", help="fail if committed schemas are stale")
    args = parser.parse_args(argv)
    if args.cmd == "schemas" and args.action == "export":
        return _export(args.out, args.check)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
