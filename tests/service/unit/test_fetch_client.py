import httpx
import respx
from research_engine.safety.http import make_fetch_client


@respx.mock
async def test_cookies_never_stored_or_replayed() -> None:
    async with make_fetch_client("UA/1") as client:
        respx.get("https://a.example/1").respond(
            200, headers={"set-cookie": "sid=abc; Path=/; Domain=a.example"}
        )
        route = respx.get("https://a.example/2").respond(200)
        await client.get("https://a.example/1")
        assert len(client.cookies) == 0
        await client.get("https://a.example/2")
        assert "cookie" not in route.calls.last.request.headers


@respx.mock
async def test_client_defaults() -> None:
    async with make_fetch_client("UA/1") as client:
        assert client.follow_redirects is False
        assert client.headers["user-agent"] == "UA/1"
        respx.get("https://a.example/r").respond(302, headers={"location": "/x"})
        resp = await client.get("https://a.example/r")
        assert resp.status_code == 302 and isinstance(client, httpx.AsyncClient)
