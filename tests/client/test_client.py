"""ResearchEngineClient against mocked HTTP (V1-13)."""

import asyncio
from typing import Any

import httpx
import pytest
import respx
from research_engine_client import ResearchEngineClient, ResearchEngineError
from research_engine_client.models import (
    BatchFetchRequest,
    ErrorCode,
    SearchReadRequest,
    SearchRequest,
)

BASE = "http://re.test"
META = {"request_id": "r" * 32, "schema_version": "1.0.0", "took_ms": 3, "cache_hit": True}
SEARCH = {
    "query": "q",
    "results": [],
    "suggestions": [],
    "infoboxes": [],
    "unresponsive_engines": [],
}
JOB: dict[str, Any] = {
    "id": "j",
    "type": "search_read",
    "status": "running",
    "progress": {"done": 0, "total": 1, "current": None},
    "parent_id": None,
    "session_id": None,
    "request": {},
    "result_ref": None,
    "errors": [],
    "created_at": "2026-10-04T00:00:00Z",
    "started_at": None,
    "finished_at": None,
}
HEALTH = {
    "status": "down",
    "version": "0.1.0",
    "git_sha": "unknown",
    "dependencies": {"database": {"state": "down", "latency_ms": None, "detail": "OSError"}},
}


def env(data: Any, errors: list[Any] | None = None) -> dict[str, Any]:
    return {"data": data, "meta": META, "errors": errors or []}


def detail(status: str) -> dict[str, Any]:
    return {"data": {"job": {**JOB, "status": status}, "result": None}, "meta": META, "errors": []}


@respx.mock
async def test_search_returns_model_and_meta() -> None:
    route = respx.post(f"{BASE}/v1/search").respond(json=env(SEARCH))
    async with ResearchEngineClient(BASE, "k") as c:
        resp = await c.search(SearchRequest(query="q"))
    assert resp.query == "q" and c.last_meta is not None and c.last_meta.cache_hit
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "k"
    assert request.headers["user-agent"].startswith("research-engine-client/")
    assert request.headers["content-type"] == "application/json"


@respx.mock
async def test_error_raises_typed() -> None:
    respx.post(f"{BASE}/v1/search").respond(
        504,
        json=env(
            None,
            [
                {
                    "code": "upstream_timeout",
                    "message": "slow",
                    "retryable": True,
                    "source": "searxng",
                }
            ],
        ),
    )
    async with ResearchEngineClient(BASE, "k") as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.search(SearchRequest(query="q"))
    assert ei.value.status == 504 and ei.value.retryable and ei.value.errors[0].source == "searxng"
    assert ei.value.errors[0].code is ErrorCode.UPSTREAM_TIMEOUT
    assert ei.value.request_id == META["request_id"]


@respx.mock
async def test_2xx_without_data_raises() -> None:
    respx.post(f"{BASE}/v1/search").respond(200, json=env(None))
    async with ResearchEngineClient(BASE, "k") as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.search(SearchRequest(query="q"))
    assert ei.value.status == 200 and ei.value.errors[0].code is ErrorCode.UPSTREAM_ERROR


@respx.mock
async def test_wait_for_job_times_out() -> None:
    respx.get(f"{BASE}/v1/jobs/j").respond(json=detail("running"))
    async with ResearchEngineClient(BASE, "k") as c:
        with pytest.raises(TimeoutError):
            await c.wait_for_job("j", timeout_s=0.01)


@respx.mock
async def test_wait_for_job_long_polls_until_terminal() -> None:
    route = respx.get(f"{BASE}/v1/jobs/j").mock(
        side_effect=[
            httpx.Response(200, json=detail("running")),
            httpx.Response(200, json=detail("done")),
        ]
    )
    async with ResearchEngineClient(BASE, "k") as c:
        got = await c.wait_for_job("j", timeout_s=900)
    assert got.job.status.is_terminal and route.call_count == 2
    assert route.calls[0].request.url.params["wait"] == "30.0"
    assert route.calls[0].request.extensions["timeout"]["read"] == 60.0  # wait + 30 s


@respx.mock
async def test_wait_for_job_never_busy_loops() -> None:
    """A server that ignores ``wait`` and answers instantly must not be hammered."""
    route = respx.get(f"{BASE}/v1/jobs/j").respond(json=detail("running"))
    async with ResearchEngineClient(BASE, "k") as c:
        with pytest.raises(TimeoutError):
            await c.wait_for_job("j", timeout_s=0.6)
    assert route.call_count <= 6


@respx.mock
async def test_wait_for_job_slice_is_capped_by_remaining() -> None:
    route = respx.get(f"{BASE}/v1/jobs/j").respond(json=detail("done"))
    async with ResearchEngineClient(BASE, "k") as c:
        await c.wait_for_job("j", timeout_s=5)
    assert 0 < float(route.calls[0].request.url.params["wait"]) <= 5


@respx.mock
async def test_get_job_without_wait_sends_no_param() -> None:
    route = respx.get(f"{BASE}/v1/jobs/j").respond(json=detail("running"))
    async with ResearchEngineClient(BASE, "k") as c:
        await c.get_job("j")
    assert "wait" not in route.calls.last.request.url.params


@respx.mock
async def test_job_methods_and_paths() -> None:
    respx.post(f"{BASE}/v1/fetch/batch").respond(202, json=env(JOB))
    respx.post(f"{BASE}/v1/search_read").respond(202, json=env(JOB))
    respx.delete(f"{BASE}/v1/jobs/j").respond(json=env({**JOB, "status": "cancelled"}))
    async with ResearchEngineClient(BASE, "k") as c:
        assert (
            await c.fetch_batch(BatchFetchRequest.model_validate({"urls": ["https://a.example/"]}))
        ).id == "j"
        req = SearchReadRequest.model_validate({"search": {"query": "q"}})
        assert (await c.search_read(req)).id == "j"
        assert (await c.cancel_job("j")).status.value == "cancelled"


async def test_non_json_error() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(502, text="Bad Gateway", headers={"X-Request-ID": "abc"})
    )
    async with ResearchEngineClient(BASE, "k", transport=transport) as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.version()
    err = ei.value
    assert err.status == 502 and err.request_id == "abc" and err.retryable
    assert err.errors[0].code is ErrorCode.UPSTREAM_ERROR and "Bad Gateway" in str(err)


async def test_invalid_envelope_4xx_not_retryable_and_truncated() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"detail": "x" * 500}))
    async with ResearchEngineClient(BASE, "k", transport=transport) as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.version()
    assert not ei.value.retryable and ei.value.request_id is None
    assert len(ei.value.errors[0].message) == 200


async def test_error_never_contains_api_key() -> None:
    secret = "sk-very-secret-key"
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text=f"bad key {secret} echoed"))
    async with ResearchEngineClient(BASE, secret, transport=transport) as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.version()
    assert secret not in str(ei.value) and secret not in repr(ei.value)
    assert secret not in ei.value.errors[0].message


async def test_repr_hides_key() -> None:
    c = ResearchEngineClient(BASE, "sk-very-secret-key")
    try:
        assert "sk-very-secret-key" not in repr(c) and "sk-very-secret-key" not in str(c)
        assert "re.test" in repr(c)
    finally:
        await c.aclose()


async def test_never_prints_key(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=env(SEARCH)))
    async with ResearchEngineClient(BASE, "sk-very-secret-key", transport=transport) as c:
        await c.search(SearchRequest(query="q"))
    out = capsys.readouterr()
    assert "sk-very-secret-key" not in out.out + out.err
    assert "sk-very-secret-key" not in caplog.text


@respx.mock
async def test_health_returns_report_on_503_with_data() -> None:
    respx.get(f"{BASE}/health").respond(503, json=env(HEALTH))
    async with ResearchEngineClient(BASE, "k") as c:
        report = await c.health()
    assert report.status.value == "down" and report.dependencies["database"].detail == "OSError"


@respx.mock
async def test_health_503_without_data_raises() -> None:
    respx.get(f"{BASE}/health").respond(
        503, json=env(None, [{"code": "internal_error", "message": "x", "retryable": True}])
    )
    async with ResearchEngineClient(BASE, "k") as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.health()
    assert ei.value.status == 503


@pytest.mark.parametrize("base", ["http://h.test/re", "http://h.test/re/", "http://h.test/re//"])
async def test_base_url_path_prefix_and_trailing_slash(base: str) -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.path)
        return httpx.Response(200, json=env(SEARCH))

    async with ResearchEngineClient(base, "k", transport=httpx.MockTransport(handler)) as c:
        await c.search(SearchRequest(query="q"))
    assert seen == ["/re/v1/search"]


@pytest.mark.parametrize("base", ["http://h.test", "http://h.test/"])
async def test_base_url_root(base: str) -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.path)
        return httpx.Response(200, json=env(SEARCH))

    async with ResearchEngineClient(base, "k", transport=httpx.MockTransport(handler)) as c:
        await c.search(SearchRequest(query="q"))
    assert seen == ["/v1/search"]


async def test_wait_for_job_cancellation_propagates() -> None:
    async def slow(r: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json=detail("running"))

    async with ResearchEngineClient(BASE, "k", transport=httpx.MockTransport(slow)) as c:
        task = asyncio.create_task(c.wait_for_job("j"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


SECRET = "sk-very-secret-key"


def _all_text(exc: BaseException) -> str:
    return repr(exc) + str(exc) + repr(vars(exc)) + repr(exc.args)


@pytest.mark.parametrize(
    ("raised", "code", "message"),
    [
        (httpx.ReadTimeout, ErrorCode.UPSTREAM_TIMEOUT, "request timed out"),
        (httpx.ConnectTimeout, ErrorCode.UPSTREAM_TIMEOUT, "request timed out"),
        (
            httpx.ConnectError,
            ErrorCode.UPSTREAM_ERROR,
            "could not reach the Research Engine: ConnectError",
        ),
        (
            httpx.RemoteProtocolError,
            ErrorCode.UPSTREAM_ERROR,
            "could not reach the Research Engine: RemoteProtocolError",
        ),
    ],
)
async def test_transport_errors_become_typed(
    raised: type[httpx.TransportError], code: ErrorCode, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised("boom", request=request)

    async with ResearchEngineClient(BASE, SECRET, transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.search(SearchRequest(query="q"))
    exc = ei.value
    assert exc.status == 0 and exc.retryable and exc.request_id is None
    assert exc.errors[0].code is code and exc.errors[0].message == message
    assert exc.__cause__ is None and exc.__suppress_context__ is True
    assert SECRET not in _all_text(exc)


async def test_server_supplied_error_text_is_redacted() -> None:
    body = env(
        None,
        [
            {
                "code": "invalid_request",
                "message": f"bad key {SECRET}",
                "retryable": False,
                "source": SECRET,
            }
        ],
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json=body))
    async with ResearchEngineClient(BASE, SECRET, transport=transport) as c:
        with pytest.raises(ResearchEngineError) as ei:
            await c.version()
    assert SECRET not in _all_text(ei.value) and "***" in ei.value.errors[0].message
    assert ei.value.errors[0].source == "***"


class ClosingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


async def test_client_owns_and_closes_injected_transport() -> None:
    transport = ClosingTransport(lambda r: httpx.Response(200, json=env(SEARCH)))
    c = ResearchEngineClient(BASE, "k", transport=transport)
    await c.aclose()
    assert transport.closed


async def test_aexit_closes_even_when_body_raises() -> None:
    transport = ClosingTransport(lambda r: httpx.Response(200, json=env(SEARCH)))
    with pytest.raises(RuntimeError):
        async with ResearchEngineClient(BASE, "k", transport=transport):
            raise RuntimeError("boom")
    assert transport.closed


@pytest.mark.parametrize(("given", "sent"), [(500, "60.0"), (-5, None), (12.5, "12.5")])
@respx.mock
async def test_get_job_clamps_wait(given: float, sent: str | None) -> None:
    route = respx.get(f"{BASE}/v1/jobs/j").respond(json=detail("running"))
    async with ResearchEngineClient(BASE, "k") as c:
        await c.get_job("j", wait_s=given)
    assert route.calls.last.request.url.params.get("wait") == sent


@respx.mock
async def test_wait_for_job_deadline_with_early_nonterminal_returns() -> None:
    respx.get(f"{BASE}/v1/jobs/j").respond(json=detail("queued"))
    loop = asyncio.get_running_loop()
    start = loop.time()
    async with ResearchEngineClient(BASE, "k") as c:
        with pytest.raises(TimeoutError):
            await c.wait_for_job("j", timeout_s=0.5)
    assert 0.4 < loop.time() - start < 2.0
