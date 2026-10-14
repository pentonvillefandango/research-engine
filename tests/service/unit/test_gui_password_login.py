import httpx
import pytest
from research_engine.gui.session import COOKIE
from research_engine.keys.password import hash_password

from tests.conftest import TEST_API_KEY, running_lifespan

ORIGIN = {"Origin": "http://research.localhost"}
PW = "correct horse battery"


@pytest.fixture
async def pw_app(settings_env, monkeypatch):
    monkeypatch.setenv("GUI_PASSWORD_HASH", hash_password(PW))
    from research_engine.app import create_app
    from research_engine.config import Settings
    from research_engine.testing import build_test_services

    settings = Settings()  # type: ignore[call-arg]
    application = create_app(settings, services=build_test_services(settings))
    async with running_lifespan(application):
        yield application


def anon(app, ip: str = "10.0.0.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5000)),
        base_url="http://research.localhost",
    )


async def test_form_shows_password_field(pw_app) -> None:
    async with anon(pw_app) as c:
        html = (await c.get("/login")).text
    assert 'name="password"' in html and 'name="api_key"' not in html
    assert "Password" in html


async def test_password_logs_in(pw_app) -> None:
    async with anon(pw_app) as c:
        r = await c.post("/login", data={"password": PW, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 303 and COOKIE in c.cookies
        assert (await c.get("/")).status_code == 200


async def test_wrong_password_and_key_refused(pw_app) -> None:
    async with anon(pw_app) as c:
        r = await c.post("/login", data={"password": "nope nope nope", "next": "/"}, headers=ORIGIN)
        assert r.status_code == 401 and "Invalid password" in r.text
        r = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 401


async def test_password_rate_limited(pw_app) -> None:
    async with anon(pw_app, ip="10.0.0.77") as c:
        for _ in range(5):
            await c.post(
                "/login", data={"password": "bad bad bad bad", "next": "/"}, headers=ORIGIN
            )
        r = await c.post("/login", data={"password": PW, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 429
