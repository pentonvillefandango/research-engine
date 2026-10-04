import asyncio
import time

from research_engine.safety.limiter import DomainLimiter


async def test_spacing_same_domain() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0.05)
    starts: list[float] = []

    async def go() -> None:
        async with lim.slot("https://a.example/x"):
            starts.append(time.monotonic())

    await asyncio.gather(go(), go())
    assert starts[1] - starts[0] >= 0.045


async def test_different_domains_parallel() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0.5)
    t0 = time.monotonic()

    async def go(u: str) -> None:
        async with lim.slot(u):
            pass

    await asyncio.gather(go("https://a.example/"), go("https://b.example/"))
    assert time.monotonic() - t0 < 0.2


def test_set_delay_capped() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=1)
    lim.set_delay("a.example", 99)
    assert lim.delay_for("a.example") == 30
    assert lim.delay_for("b.example") == 1


def test_set_delay_never_lowers_below_default() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=2)
    lim.set_delay("a.example", 0.5)
    assert lim.delay_for("a.example") == 2


async def test_concurrency_bound() -> None:
    lim = DomainLimiter(concurrency=2, delay_s=0)
    running = peak = 0

    async def go() -> None:
        nonlocal running, peak
        async with lim.slot("https://a.example/"):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(go() for _ in range(6)))
    assert peak == 2


async def test_state_is_bounded() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0, max_domains=5)
    for i in range(50):
        async with lim.slot(f"https://d{i}.example/"):
            pass
    assert lim.tracked_domains <= 5


async def test_eviction_never_drops_domain_in_use() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0, max_domains=2)
    async with lim.slot("https://held.example/"):
        for i in range(10):
            async with lim.slot(f"https://d{i}.example/"):
                pass
        assert lim.tracked_domains <= 3  # held domain survives, plus at most the cap's worth
        # held.example is still tracked, so a second request there still queues behind it
        second = asyncio.create_task(_enter(lim, "https://held.example/"))
        await asyncio.sleep(0.02)
        assert not second.done()
    await second


async def _enter(lim: DomainLimiter, url: str) -> None:
    async with lim.slot(url):
        pass


async def test_crawl_delay_survives_while_tracked() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0, max_domains=3)
    async with lim.slot("https://a.example/"):
        pass
    lim.set_delay("a.example", 7)
    assert lim.delay_for("a.example") == 7


async def test_idn_host_shares_key_with_punycode() -> None:
    """robots.txt sets the delay on the punycode host; the slot for the IDN URL must honour it."""
    lim = DomainLimiter(concurrency=1, delay_s=0)
    lim.set_delay("xn--bcher-kva.example", 0.05)
    starts: list[float] = []
    for _ in range(2):
        async with lim.slot("https://Bücher.example/x"):
            starts.append(time.monotonic())
    assert starts[1] - starts[0] >= 0.045
    assert lim.tracked_domains == 1
