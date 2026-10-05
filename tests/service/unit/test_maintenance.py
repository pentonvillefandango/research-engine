import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from research_engine import app as app_mod
from research_engine import maintenance
from research_engine.api.deps import Services
from research_engine.config import Settings
from research_engine.testing import build_test_services
from research_engine_client.models import Event, EventKind, EventLevel
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture(params=["memory", "file"])
async def services(
    request: pytest.FixtureRequest, settings_env: None, tmp_path: Path
) -> AsyncIterator[Services]:
    path = ":memory:" if request.param == "memory" else str(tmp_path / "m.db")
    s = build_test_services(Settings(db_path=path))  # type: ignore[call-arg]
    from research_engine.store.db import init_db

    await init_db(s.engine)
    yield s
    await s.engine.dispose()


async def test_run_once_prunes_events_and_cache(services: Services) -> None:
    old = datetime.now(UTC) - timedelta(days=services.settings.event_retention_days + 1)
    await services.events.emit(
        Event(ts=old, level=EventLevel.INFO, kind=EventKind.SYSTEM_STARTUP, message="old", data={})
    )
    await services.events.emit(
        Event(
            ts=datetime.now(UTC),
            level=EventLevel.INFO,
            kind=EventKind.SYSTEM_STARTUP,
            message="new",
            data={},
        )
    )
    await services.cache.set("expired", b"x", ttl_s=-1)
    await services.cache.set("fresh", b"y", ttl_s=600)
    assert await maintenance.run_once(services) == {"events_pruned": 1, "cache_pruned": 1}
    assert [e.message for e in await services.events.tail(10)] == ["new"]
    assert await services.cache.get("fresh") == b"y"


async def test_loop_survives_exceptions_and_logs(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def flaky(_s: Services) -> dict[str, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db hiccup")
        return {"events_pruned": 0, "cache_pruned": 0}

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(maintenance, "run_once", flaky)
    errors: list[str] = []
    monkeypatch.setattr(
        maintenance._log,  # pyright: ignore[reportPrivateUsage]
        "exception",
        lambda msg, **_k: errors.append(msg),
    )
    with pytest.raises(asyncio.CancelledError):
        await maintenance.maintenance_loop(services, interval_s=7, sleep=fake_sleep)
    assert calls == 3 and sleeps == [7, 7, 7]
    assert len(errors) == 1


async def _lifespan_app(
    settings_env: None, monkeypatch: pytest.MonkeyPatch, *, owned: bool
) -> tuple[FastAPI, Services]:
    settings = Settings()  # type: ignore[call-arg]
    services = build_test_services(settings)
    services.http = httpx.AsyncClient()
    services.fetch_http = httpx.AsyncClient()
    if owned:
        monkeypatch.setattr(app_mod, "build_services", lambda _s: services)
        return app_mod.create_app(settings), services
    return app_mod.create_app(settings, services=services), services


async def test_startup_and_shutdown_events(
    settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, services = await _lifespan_app(settings_env, monkeypatch, owned=True)
    async with app.router.lifespan_context(app):
        kinds = [e.kind for e in await services.events.tail(10)]
        assert kinds == [EventKind.SYSTEM_STARTUP]
        start = (await services.events.tail(1))[0]
        assert start.data["version"] and "git_sha" in start.data
    assert services.http and services.http.is_closed
    assert services.fetch_http and services.fetch_http.is_closed


async def test_shutdown_steps_all_run_when_earlier_ones_raise(
    settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, services = await _lifespan_app(settings_env, monkeypatch, owned=True)
    order: list[str] = []
    real_dispose = AsyncEngine.dispose

    async def bad_stop() -> None:
        order.append("stop")
        raise RuntimeError("stop failed")

    async def dispose() -> None:
        order.append("dispose")
        await real_dispose(services.engine)

    real_emit = services.events.emit

    async def emit(event: Event) -> None:
        if event.kind == EventKind.SYSTEM_SHUTDOWN:
            order.append("emit")
            raise RuntimeError("emit failed")
        await real_emit(event)

    monkeypatch.setattr(services.events, "emit", emit)
    monkeypatch.setattr(services.jobs, "stop", bad_stop)
    monkeypatch.setattr(AsyncEngine, "dispose", lambda _self: dispose())
    with pytest.raises(RuntimeError, match="emit failed"):
        async with app.router.lifespan_context(app):
            pass
    assert order == ["emit", "stop", "dispose"]
    assert services.http and services.http.is_closed
    assert services.fetch_http and services.fetch_http.is_closed
