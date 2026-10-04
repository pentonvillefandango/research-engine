"""Record real upstream responses as test fixtures. Run against the dev stack:

source <(scripts/dev_urls.sh) && uv run python scripts/record_fixtures.py searxng
"""

import json
import os
import sys
from pathlib import Path

import httpx

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def record_searxng() -> None:
    base = os.environ["SEARXNG_LIVE_URL"]
    out = FIX / "searxng"
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, timeout=30) as c:
        cfg = c.get("/config").json()
        enabled = sorted(e["name"] for e in cfg["engines"] if e["enabled"])
        (out / "engines_config.json").write_text(json.dumps({"enabled": enabled}, indent=2) + "\n")
        for name, params in {
            "technical_page1": {
                "q": "python asyncio TaskGroup exception handling",
                "engines": "github,stackoverflow,mdn,duckduckgo,brave,bing",
            },
            "general_page1": {"q": "compare open-source vector databases", "categories": "general"},
        }.items():
            r = c.get(
                "/search", params={**params, "format": "json", "language": "en-GB", "pageno": 1}
            )
            r.raise_for_status()
            (out / f"{name}.json").write_text(json.dumps(r.json(), indent=2, sort_keys=True) + "\n")
    print("recorded searxng fixtures in", out)


if __name__ == "__main__":
    {"searxng": record_searxng}[sys.argv[1]]()
