"""search_read acceptance against the live dev stack (SearXNG + Crawl4AI) and the public web."""

import httpx
import pytest

pytestmark = pytest.mark.integration


async def test_search_read_vector_databases(live_client: httpx.AsyncClient) -> None:
    r = await live_client.post(
        "/v1/search_read",
        json={"search": {"query": "compare open-source vector databases"}, "top_n": 3},
    )
    assert r.status_code == 202
    detail = (await live_client.get(r.headers["location"], params={"wait": 60})).json()["data"]
    for _ in range(4):  # a slow page can outlast one 60 s long-poll
        if detail["job"]["status"] not in ("queued", "running"):
            break
        detail = (await live_client.get(r.headers["location"], params={"wait": 60})).json()["data"]
    job, result = detail["job"], detail["result"]
    docs = result["documents"]
    print(
        f"STATUS={job['status']} DOCS={len(docs)} "
        f"WORDS={[d['document']['word_count'] for d in docs]} "
        f"RANKS={[d['search_rank'] for d in docs]} FAILED={len(result['failed'])}"
    )
    assert job["status"] in ("done", "partial"), job["errors"]
    assert len(docs) >= 2
    assert all(d["document"]["word_count"] >= 100 for d in docs)
    assert [d["search_rank"] for d in docs] == sorted(d["search_rank"] for d in docs)


async def test_health_reports_live_dependencies_up(live_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=live_app), base_url="http://research.localhost"
    ) as c:
        r = await c.get("/health")
    deps = r.json()["data"]["dependencies"]
    assert r.status_code == 200
    assert deps["searxng"]["state"] == "up" and deps["crawl4ai"]["state"] == "up"
    assert deps["database"]["state"] == "up"
