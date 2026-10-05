import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from research_engine.events.base import Emitter, EventBus
from research_engine.events.sqlite import SqliteEventBus
from research_engine.store.db import create_engine_for, init_db
from research_engine_client.models import Event, EventKind, EventLevel


def _ids(events: list[Event]) -> list[int]:
    return [e.id for e in events if e.id is not None]


@pytest.fixture
async def bus() -> AsyncIterator[SqliteEventBus]:
    engine = create_engine_for(":memory:")
    await init_db(engine)
    yield SqliteEventBus(engine)
    await engine.dispose()


async def test_emit_persists_and_fans_out_with_id(bus: SqliteEventBus) -> None:
    got: list[Event] = []

    async def consume() -> None:
        async for e in bus.subscribe():
            got.append(e)
            return

    t = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await Emitter(bus, "j1").info(EventKind.JOB_STARTED, "go")
    await asyncio.wait_for(t, 1)
    assert got[0].id is not None and got[0].job_id == "j1"
    assert (await bus.query())[0].id == got[0].id


async def test_subscribe_returns_eager_subscription_satisfying_event_bus(
    bus: SqliteEventBus,
) -> None:
    typed: EventBus = bus  # pyright checks the protocol here
    sub = typed.subscribe()
    assert bus.subscriber_count == 1  # registered eagerly, before iteration
    await Emitter(typed).info(EventKind.SYSTEM_HEALTH, "x")
    assert (await asyncio.wait_for(sub.__anext__(), 1)).message == "x"
    await sub.aclose()
    assert bus.subscriber_count == 0


async def test_query_filters(bus: SqliteEventBus) -> None:
    em = Emitter(bus)
    await em.debug(EventKind.FETCH_STARTED, "fetch a")
    await em.warning(EventKind.SEARCH_ENGINE_FAILED, "Bing failed")
    await em.bind("j2").error(EventKind.FETCH_FAILED, "fetch b broke")
    assert [e.message for e in await bus.query(level=EventLevel.WARNING)] == [
        "Bing failed",
        "fetch b broke",
    ]
    assert [e.message for e in await bus.query(kind_prefix="fetch.")] == [
        "fetch a",
        "fetch b broke",
    ]
    assert [e.message for e in await bus.query(job_id="j2")] == ["fetch b broke"]
    assert [e.message for e in await bus.query(text="BING")] == ["Bing failed"]
    first = (await bus.query())[0].id
    assert len(await bus.query(after_id=first)) == 2


async def test_query_text_escapes_like_wildcards(bus: SqliteEventBus) -> None:
    em = Emitter(bus)
    for m in ("saved 50% off", "saved 500 off", "a_b", "axb", r"back\slash", "plain"):
        await em.info(EventKind.SYSTEM_HEALTH, m)
    assert [e.message for e in await bus.query(text="50%")] == ["saved 50% off"]
    assert [e.message for e in await bus.query(text="a_b")] == ["a_b"]
    assert [e.message for e in await bus.query(text="\\s")] == [r"back\slash"]
    assert [e.message for e in await bus.query(text="%")] == ["saved 50% off"]


async def test_query_limit_is_capped(bus: SqliteEventBus) -> None:
    em = Emitter(bus)
    for i in range(3):
        await em.info(EventKind.SYSTEM_HEALTH, f"m{i}")
    assert len(await bus.query(limit=2)) == 2
    assert len(await bus.query(limit=10_000)) == 3


async def test_tail_returns_newest_in_ascending_order(bus: SqliteEventBus) -> None:
    em = Emitter(bus)
    for i in range(3):
        await em.info(EventKind.SYSTEM_HEALTH, f"m{i}")
    tail = await bus.tail(2)
    assert [e.message for e in tail] == ["m1", "m2"]
    assert tail[0].id is not None and tail[1].id is not None and tail[0].id < tail[1].id
    assert [e.message for e in await bus.tail(10)] == ["m0", "m1", "m2"]


async def test_query_newest_returns_last_matches_in_ascending_order(bus: SqliteEventBus) -> None:
    for i in range(6):
        await Emitter(bus).info(EventKind.JOB_PROGRESS if i % 2 else EventKind.CACHE_MISS, f"m{i}")
    newest = await bus.query(kind_prefix="job.", limit=2, newest=True)
    assert [e.message for e in newest] == ["m3", "m5"]
    oldest = await bus.query(kind_prefix="job.", limit=2)
    assert [e.message for e in oldest] == ["m1", "m3"]


async def test_non_utc_timestamp_round_trips_as_same_instant(bus: SqliteEventBus) -> None:
    tz = timezone(timedelta(hours=5, minutes=30))
    ts = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=tz)
    await bus.emit(Event(ts=ts, level=EventLevel.INFO, kind=EventKind.SYSTEM_HEALTH, message="tz"))
    (stored,) = await bus.query()
    assert stored.ts == ts
    assert stored.ts.utcoffset() == timedelta(0)
    assert stored.ts.hour == 6 and stored.ts.minute == 30


async def test_prune(bus: SqliteEventBus) -> None:
    old = Event(
        ts=datetime.now(UTC) - timedelta(days=40),
        level=EventLevel.INFO,
        kind=EventKind.SYSTEM_HEALTH,
        message="old",
    )
    await bus.emit(old)
    await Emitter(bus).info(EventKind.SYSTEM_HEALTH, "new")
    assert await bus.prune(older_than_days=30) == 1
    assert [e.message for e in await bus.query()] == ["new"]


async def test_concurrent_emits_unique_ids(bus: SqliteEventBus) -> None:
    em = Emitter(bus)
    await asyncio.gather(*(em.info(EventKind.SYSTEM_HEALTH, f"m{i}") for i in range(50)))
    ids = _ids(await bus.query(limit=100))
    assert len(ids) == 50 and ids == sorted(set(ids))


async def test_concurrent_emits_on_file_database(tmp_path: Path) -> None:
    engine = create_engine_for(str(tmp_path / "ev.sqlite"))
    try:
        await init_db(engine)
        bus = SqliteEventBus(engine)
        em = Emitter(bus)
        await asyncio.gather(*(em.info(EventKind.SYSTEM_HEALTH, f"m{i}") for i in range(50)))
        ids = _ids(await bus.query(limit=100))
        assert len(ids) == 50 and ids == sorted(set(ids))
    finally:
        await engine.dispose()


@pytest.fixture(params=["memory", "file"])
async def counting_bus(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[SqliteEventBus]:
    engine = create_engine_for(":memory:" if request.param == "memory" else str(tmp_path / "e.db"))
    await init_db(engine)
    yield SqliteEventBus(engine)
    await engine.dispose()


async def test_count_matches_query_filters(counting_bus: SqliteEventBus) -> None:
    bus = counting_bus
    assert await bus.count() == 0
    a, b = Emitter(bus, "ja"), Emitter(bus, "jb")
    await a.debug(EventKind.FETCH_STARTED, "100%_sure")
    await a.info(EventKind.FETCH_DONE, "Hello World")
    await a.warning(EventKind.FETCH_FAILED, "bad")
    await b.error(EventKind.JOB_FAILED, "boom")
    await Emitter(bus).info(EventKind.SYSTEM_STARTUP, "up")
    filters: list[dict[str, object]] = [
        {},
        {"job_id": "ja"},
        {"job_id": "jb"},
        {"job_id": "nope"},
        {"level": EventLevel.WARNING},
        {"job_id": "ja", "level": EventLevel.INFO},
        {"kind_prefix": "fetch."},
        {"kind_prefix": "%"},
        {"text": "hello"},  # case-insensitive
        {"text": "100%_"},  # wildcards are literal
        {"job_id": "ja", "kind_prefix": "fetch.", "text": "BAD"},
    ]
    for f in filters:
        assert await bus.count(**f) == len(await bus.query(limit=1000, **f)), f  # type: ignore[arg-type]
    assert await bus.count() == 5 and await bus.count(job_id="ja") == 3
