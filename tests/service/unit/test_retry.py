import asyncio

import pytest
from research_engine.errors import ServiceError
from research_engine.retry import retry
from research_engine_client.models import ErrorCode


async def _nosleep(_: float) -> None:
    return None


def _retryable() -> ServiceError:
    return ServiceError.of(ErrorCode.UPSTREAM_ERROR, "x", retryable=True)


async def test_retries_then_succeeds() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _retryable()
        return "ok"

    assert await retry(flaky, attempts=3, sleep=_nosleep) == "ok" and calls == 3


async def test_non_retryable_raises_immediately() -> None:
    calls = 0

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise ServiceError.of(ErrorCode.SSRF_BLOCKED, "x", retryable=False)

    with pytest.raises(ServiceError):
        await retry(bad, attempts=3, sleep=_nosleep)
    assert calls == 1


async def test_exhausted_reraises_last_error_and_sleeps_only_between_attempts() -> None:
    sleeps: list[float] = []
    calls = 0

    async def rec(s: float) -> None:
        sleeps.append(s)

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, f"attempt {calls}", retryable=True)

    with pytest.raises(ServiceError, match="attempt 3"):
        await retry(bad, attempts=3, base_delay_s=1.0, sleep=rec)
    assert calls == 3 and len(sleeps) == 2  # no sleep after the final failure
    assert 0.8 <= sleeps[0] <= 1.2 and 1.6 <= sleeps[1] <= 2.4  # base * 2^i, +/-20%


async def test_single_attempt_never_sleeps() -> None:
    async def boom(_: float) -> None:
        raise AssertionError("slept")

    async def bad() -> None:
        raise _retryable()

    with pytest.raises(ServiceError):
        await retry(bad, attempts=1, sleep=boom)


async def test_non_service_errors_propagate_unretried() -> None:
    calls = 0

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        await retry(bad, attempts=3, sleep=_nosleep)
    assert calls == 1


async def test_cancellation_during_fn_is_not_retried() -> None:
    calls = 0
    started = asyncio.Event()

    async def hang() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(retry(hang, attempts=3, sleep=_nosleep))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


async def test_cancellation_during_backoff_sleep_propagates_immediately() -> None:
    calls = 0
    sleeping = asyncio.Event()

    async def slow_sleep(_: float) -> None:
        sleeping.set()
        await asyncio.sleep(10)

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise _retryable()

    task = asyncio.create_task(retry(bad, attempts=3, sleep=slow_sleep))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


async def test_retry_if_veto_stops_retrying() -> None:
    calls = 0

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise _retryable()

    with pytest.raises(ServiceError):
        await retry(bad, attempts=3, sleep=_nosleep, retry_if=lambda _e: False)
    assert calls == 1
