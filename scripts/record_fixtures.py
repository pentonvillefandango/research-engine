"""Record real upstream responses as test fixtures. Run against the dev stack:

source <(scripts/dev_urls.sh) && uv run python scripts/record_fixtures.py searxng
For crawl4ai, export CRAWL4AI_API_TOKEN from .env in your shell first; it is never written out.
"""

import json
import os
import re
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
                "engines": "stackoverflow,mdn,microsoft learn,github,duckduckgo,bing,yep,yahoo",
            },
            "general_page1": {"q": "compare open-source vector databases", "categories": "general"},
        }.items():
            r = c.get(
                "/search", params={**params, "format": "json", "language": "en-GB", "pageno": 1}
            )
            r.raise_for_status()
            (out / f"{name}.json").write_text(json.dumps(r.json(), indent=2, sort_keys=True) + "\n")
    print("recorded searxng fixtures in", out)


# Response headers that can identify the host or its users; dropped from recorded fixtures.
_DROP_HEADERS = {
    "server",
    "set-cookie",
    "via",
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "cf-ray",
}
_IP_LIKE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}\b", re.I)


def _scrub_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _DROP_HEADERS and "ip" not in k.lower() and not _IP_LIKE.search(v)
    }


def record_crawl4ai() -> None:
    base = os.environ["CRAWL4AI_LIVE_URL"]
    token = os.environ["CRAWL4AI_API_TOKEN"]
    out = FIX / "crawl4ai"
    out.mkdir(parents=True, exist_ok=True)
    body = {
        "urls": ["https://example.com"],
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": {"cache_mode": "bypass"}},
    }
    r = httpx.post(
        f"{base}/crawl", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=120
    )
    r.raise_for_status()
    data = r.json()
    for res in data.get("results", []):
        res.pop("screenshot", None)
        res.pop("pdf", None)
        if isinstance(res.get("response_headers"), dict):
            res["response_headers"] = _scrub_headers(res["response_headers"])
    (out / "crawl_example.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print("recorded crawl4ai fixture in", out)


if __name__ == "__main__":
    {"searxng": record_searxng, "crawl4ai": record_crawl4ai}[sys.argv[1]]()
