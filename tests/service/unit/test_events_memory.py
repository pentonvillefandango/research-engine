import asyncio
import gc
import json
import logging
from collections.abc import AsyncIterator, Iterator

import pytest
import structlog
from research_engine.events.base import Emitter, EventSink, EventSubscriber
from research_engine.events.memory import InMemoryEventBus
from research_engine_client.models import Event, EventKind, EventLevel


async def _next(it: AsyncIterator[Event]) -> Event:
    return await it.__anext__()


async def _collect(bus: InMemoryEventBus, n: int, out: list[Event]) -> None:
    async for ev in bus.subscribe():
        out.append(ev)
        if len(out) == n:
            return


async def test_emitter_builds_event() -> None:
    bus = InMemoryEventBus()
    got: list[Event] = []
    task = asyncio.create_task(_collect(bus, 1, got))
    await asyncio.sleep(0)
    await Emitter(bus, job_id="j").info(EventKind.JOB_STARTED, "go", n=1)
    await asyncio.wait_for(task, 1)
    ev = got[0]
    assert (ev.level, ev.kind, ev.job_id, ev.data) == (
        EventLevel.INFO,
        EventKind.JOB_STARTED,
        "j",
        {"n": 1},
    )
    assert ev.ts.tzinfo is not None


async def test_fan_out_in_order() -> None:
    bus = InMemoryEventBus()
    a: list[Event] = []
    b: list[Event] = []
    ta = asyncio.create_task(_collect(bus, 3, a))
    tb = asyncio.create_task(_collect(bus, 3, b))
    await asyncio.sleep(0)
    em = Emitter(bus)
    for i in range(3):
        await em.info(EventKind.SYSTEM_HEALTH, f"m{i}")
    await asyncio.wait_for(asyncio.gather(ta, tb), 1)
    assert [e.message for e in a] == [e.message for e in b] == ["m0", "m1", "m2"]


async def test_bounded_queue_drops_oldest() -> None:
    bus = InMemoryEventBus(max_queue=2)
    it = bus.subscribe().__aiter__()
    first = asyncio.create_task(_next(it))
    await asyncio.sleep(0)
    em = Emitter(bus)
    for i in range(5):
        await em.info(EventKind.SYSTEM_HEALTH, f"m{i}")
    got = [(await asyncio.wait_for(first, 1)).message]
    got.append((await asyncio.wait_for(it.__anext__(), 1)).message)
    assert got[-1] == "m4"  # newest retained; oldest dropped


async def test_default_queue_bound_is_1000() -> None:
    bus = InMemoryEventBus()
    it = bus.subscribe().__aiter__()
    first = asyncio.create_task(_next(it))
    await asyncio.sleep(0)
    em = Emitter(bus)
    for i in range(1500):
        await em.debug(EventKind.SYSTEM_HEALTH, f"m{i}")
    msgs = [(await asyncio.wait_for(first, 1)).message]
    for _ in range(999):
        msgs.append((await asyncio.wait_for(it.__anext__(), 1)).message)
    assert msgs[-1] == "m1499"
    assert len(msgs) == 1000


async def test_unsubscribe_on_cancel() -> None:
    bus = InMemoryEventBus()
    task = asyncio.create_task(_collect(bus, 99, []))
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert bus.subscriber_count == 0


@pytest.fixture
def _restore_structlog() -> Iterator[None]:
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    yield
    # configure_logging("DEBUG") is process-global (structlog and the stdlib root logger):
    # without a reset every later test would log at DEBUG, which slows the suite and trips
    # timing tests.
    structlog.reset_defaults()
    root.setLevel(level)
    root.handlers[:] = handlers


@pytest.mark.usefixtures("_restore_structlog")
async def test_events_are_logged_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    from research_engine.logging import configure_logging

    configure_logging("DEBUG")
    await Emitter(InMemoryEventBus(), job_id="jl").warning(
        EventKind.SEARCH_ENGINE_FAILED, "boom", engine="bing"
    )
    out = capsys.readouterr().out
    rec = json.loads(out.strip().splitlines()[-1])
    assert (rec["event_kind"], rec["job_id"], rec["level"]) == (
        "search.engine_failed",
        "jl",
        "warning",
    )
    assert rec["data"] == {"engine": "bing"}


async def test_subscribe_registers_immediately() -> None:
    bus = InMemoryEventBus()
    it = bus.subscribe()
    assert bus.subscriber_count == 1
    await Emitter(bus).info(EventKind.SYSTEM_HEALTH, "early")
    assert (await asyncio.wait_for(anext(it), 1)).message == "early"
    await it.aclose()
    assert bus.subscriber_count == 0


async def test_aclose_before_first_iteration_deregisters() -> None:
    bus = InMemoryEventBus()
    it = bus.subscribe()
    assert bus.subscriber_count == 1
    await it.aclose()
    assert bus.subscriber_count == 0
    await it.aclose()  # idempotent
    with pytest.raises(StopAsyncIteration):
        await anext(it)


async def test_emitter_data_keys_do_not_collide_with_log_fields() -> None:
    bus = InMemoryEventBus()
    it = bus.subscribe()
    await Emitter(bus, job_id="j").info(EventKind.SYSTEM_HEALTH, "m", event="x", level="y")
    ev = await asyncio.wait_for(anext(it), 1)
    assert ev.data == {"event": "x", "level": "y"} and ev.level is EventLevel.INFO
    await it.aclose()


async def test_dropped_unclosed_subscription_is_collected() -> None:
    bus = InMemoryEventBus()
    it = bus.subscribe()
    assert bus.subscriber_count == 1
    del it
    gc.collect()
    assert bus.subscriber_count == 0


def test_bus_satisfies_protocols() -> None:
    sub: EventSubscriber = InMemoryEventBus()
    sink: EventSink = InMemoryEventBus()
    assert sub is not None and sink is not None
