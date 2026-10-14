"""Keys page (V1-24): GUI-session only create/revoke/audit of named API keys."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from research_engine.keys.usage import RequestRecord

from tests.conftest import TEST_API_KEY
from tests.service.unit.test_gui_auth import ORIGIN, _login, anon


@asynccontextmanager
async def gui(app) -> AsyncIterator[httpx.AsyncClient]:
    async with anon(app) as c:
        assert (await _login(c)).status_code == 303
        yield c


def _rec(key_id: int, n: int, status: int = 200) -> RequestRecord:
    return RequestRecord(
        ts=datetime.now(UTC),
        caller_kind="key",
        key_id=key_id,
        caller_name="logged",
        method="POST",
        path=f"/v1/row{n:03d}",
        tool=None,
        status=status,
        duration_ms=3,
        client_ip="9.9.9.9",
    )


async def test_page_lists_env_first(app) -> None:
    async with gui(app) as c:
        r = await c.get("/keys")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "env (built-in)" in r.text
    assert '<a href="/keys">Keys</a>' in r.text


async def test_order_env_active_revoked(app) -> None:
    keys = app.state.services.keys
    gone, _ = await keys.create("zz-gone")
    await keys.create("aa-live")
    await keys.revoke(gone.id)
    async with gui(app) as c:
        text = (await c.get("/keys")).text
    assert text.index("env (built-in)") < text.index("aa-live") < text.index("zz-gone")
    assert re.search(r'<tr class="revoked">\s*<td>zz-gone', text)


async def test_api_key_cannot_manage_keys(app) -> None:
    _, named = await app.state.services.keys.create("app")
    for key in (TEST_API_KEY, named):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://research.localhost",
            headers={"X-API-Key": key, **ORIGIN},
        ) as c:
            assert (await c.get("/keys")).status_code == 403
            assert (await c.post("/keys", data={"name": "evil"})).status_code == 403
            assert (await c.get("/keys/requests")).status_code == 403


async def test_unauthenticated_redirects(app) -> None:
    async with anon(app) as c:
        r = await c.get("/keys")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


async def test_cross_origin_post_forbidden(app) -> None:
    async with gui(app) as c:
        r = await c.post("/keys", data={"name": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


async def test_create_shows_key_once_and_it_works(app) -> None:
    async with gui(app) as c:
        r = await c.post("/keys", data={"name": "newsdesk"}, headers=ORIGIN)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        m = re.search(r'<code id="new-key">(re_[A-Za-z0-9_-]{43})</code>', r.text)
        assert m
        key = m.group(1)
        again = await c.get("/keys")
        assert key not in again.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://research.localhost",
        headers={"X-API-Key": key},
    ) as api:
        ok = await api.post("/v1/search", json={"query": "x", "depth": "quick"})
        assert ok.status_code == 200


async def test_duplicate_and_escape(app) -> None:
    async with gui(app) as c:
        await c.post("/keys", data={"name": "<script>x</script>"}, headers=ORIGIN)
        r = await c.post("/keys", data={"name": "<script>x</script>"}, headers=ORIGIN)
        assert r.status_code == 400 and 'role="alert"' in r.text
        page = (await c.get("/keys")).text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert len(await app.state.services.keys.list_keys()) == 1


async def test_revoke(app) -> None:
    row, key = await app.state.services.keys.create("tmp")
    async with gui(app) as c:
        r = await c.post(f"/keys/{row.id}/revoke", headers=ORIGIN)
        assert r.status_code == 303 and r.headers["location"] == "/keys"
        assert (await c.post(f"/keys/{row.id}/revoke", headers=ORIGIN)).status_code == 404
        assert (await c.post("/keys/9999/revoke", headers=ORIGIN)).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://research.localhost",
        headers={"X-API-Key": key},
    ) as api:
        assert (await api.post("/v1/search", json={"query": "x"})).status_code == 401


async def test_requests_partial_filters_and_pages(app) -> None:
    keys = app.state.services.keys
    usage = app.state.services.usage
    row, _ = await keys.create("busy")
    other, _ = await keys.create("quiet")
    for n in range(51):
        usage.record(_rec(row.id, n, 500 if n == 3 else 200))
    usage.record(_rec(other.id, 999))
    await usage.flush()
    async with gui(app) as c:
        r = await c.get(f"/keys/requests?caller=key:{row.id}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        paths = re.findall(r"/v1/row(\d{3})", r.text)
        assert paths == [f"{n:03d}" for n in range(50, 0, -1)]
        assert "999" not in paths
        older = re.search(r'hx-get="/keys/requests\?before=(\d+)[^"]*"', r.text)
        assert older
        before = older.group(1)
        r2 = await c.get(f"/keys/requests?caller=key:{row.id}&before={before}")
        assert re.findall(r"/v1/row(\d{3})", r2.text) == ["000"]
        assert "Older" not in r2.text
        err = await c.get(f"/keys/requests?caller=key:{row.id}&errors=1")
        assert re.findall(r"/v1/row(\d{3})", err.text) == ["003"]
        assert 'class="error"' in err.text


async def test_requests_partial_rejects_unknown_caller(app) -> None:
    async with gui(app) as c:
        for bad in ("bogus", "key:", "key:1x", "key:-1", "ENV"):
            r = await c.get("/keys/requests", params={"caller": bad})
            assert r.status_code == 400, bad
        for ok in ("", "env", "gui", "key:12"):
            r = await c.get("/keys/requests", params={"caller": ok})
            assert r.status_code == 200, ok


async def test_session_with_api_key_header_is_refused(app) -> None:
    async with gui(app) as c:
        r = await c.get("/keys", headers={"X-API-Key": "bogus"})
    assert r.status_code == 403


async def test_huge_ids_are_422_not_500(app) -> None:
    big = 2**63
    async with gui(app) as c:
        r1 = await c.get(f"/keys/requests?before={big}")
        r2 = await c.post(f"/keys/{big}/revoke", headers=ORIGIN)
    assert r1.status_code == 422
    assert r2.status_code == 422
