"""Search, fetch the top result and print its provenance with the typed client.

    RESEARCH_ENGINE_API_KEY=... uv run python examples/client_usage.py

Reads RESEARCH_ENGINE_URL (default https://research.localhost) and RESEARCH_ENGINE_API_KEY.
The key is never printed.
"""

import asyncio
import os
import sys

from research_engine_client import ResearchEngineClient, ResearchEngineError
from research_engine_client.models import FetchRequest, SearchRequest

DEFAULT_URL = "https://research.localhost"


async def main() -> int:
    api_key = os.environ.get("RESEARCH_ENGINE_API_KEY")
    if not api_key:
        print("error: set RESEARCH_ENGINE_API_KEY", file=sys.stderr)
        return 2
    base_url = os.environ.get("RESEARCH_ENGINE_URL", DEFAULT_URL)
    try:
        async with ResearchEngineClient(base_url, api_key) as client:
            found = await client.search(SearchRequest(query="vector database", max_results=5))
            if not found.results:
                print("error: the search returned no results", file=sys.stderr)
                return 1
            top = found.results[0]
            print(f"top result: {top.title} ({top.url})")
            doc = await client.fetch(FetchRequest(url=top.url))
    except ResearchEngineError as exc:
        # The message holds the status and the typed errors; the key is never part of it.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    prov = doc.provenance
    print(f"title: {doc.title or '(none)'}")
    print(f"words: {doc.word_count}")
    print(
        f"provenance: url={prov.url} fetched_at={prov.fetched_at.isoformat()} "
        f"method={prov.method.value} content_hash={prov.content_hash}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
