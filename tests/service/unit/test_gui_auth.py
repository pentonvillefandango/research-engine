import hmac
import re
import time
from pathlib import Path

import httpx
import pytest
from itsdangerous import TimestampSigner
from research_engine.api import auth as auth_mod
from research_engine.gui.session import (
    COOKIE,
    MAX_AGE_S,
    SALT,
    LoginRateLimiter,
    SessionCodec,
    safe_next,
    same_origin,
)
from research_engine_client.models import ErrorCode

from tests.conftest import TEST_API_KEY, TEST_ENV

ORIGIN = {"Origin": "http://research.localhost"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
TEMPLATES = (
    Path(__file__).resolve().parents[3]
    / "packages/research_engine/src/research_engine/gui/templates"
)


def anon(app, *, base_url: str = "http://research.localhost", ip: str = "10.0.0.1"):
    transport = httpx.ASGITransport(app=app, client=(ip, 51234))
    return httpx.AsyncClient(transport=transport, base_url=base_url)


async def _login(c: httpx.AsyncClient, key: str = TEST_API_KEY) -> httpx.Response:
    return await c.post("/login", data={"api_key": key, "next": "/"}, headers=ORIGIN)


async def _cookie(app) -> str:
    async with anon(app) as c:
        r = await _login(c)
        assert r.status_code == 303
        return c.cookies[COOKIE]


# --- brief tests -----------------------------------------------------------------------------


async def test_redirects_to_login(app) -> None:
    async with anon(app) as c:
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
        assert (await c.post("/v1/search", json={"query": "x"})).status_code == 401


async def test_login_sets_cookie_and_authenticates_api(app) -> None:
    async with anon(app) as c:
        r = await _login(c)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie
        ok = await c.post("/v1/search", json={"query": "x", "depth": "quick"}, headers=ORIGIN)
        assert ok.status_code == 200
        bad = await c.post(
            "/v1/search", json={"query": "x"}, headers={"Origin": "http://evil.example"}
        )
        assert bad.status_code == 403


async def test_open_redirect_blocked(app) -> None:
    async with anon(app) as c:
        r = await c.post(
            "/login", data={"api_key": TEST_API_KEY, "next": "//evil.example/"}, headers=ORIGIN
        )
        assert r.headers["location"] == "/"


async def test_bad_key_and_rate_limit(app) -> None:
    async with anon(app) as c:
        responses = [await _login(c, "wrong") for _ in range(6)]
    codes = [r.status_code for r in responses]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert "Invalid key" in responses[0].text and 'name="api_key"' in responses[0].text


async def test_tampered_cookie_rejected(app) -> None:
    async with anon(app) as c:
        c.cookies.set("re_session", "gui.forged.sig")
        assert (await c.get("/")).status_code == 303


async def test_api_key_ignores_origin(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/search",
        json={"query": "x", "depth": "quick"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 200


async def test_csp_header(app) -> None:
    async with anon(app) as c:
        r = await c.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


# --- login page and cookie -------------------------------------------------------------------


async def test_login_page_renders_form(app) -> None:
    async with anon(app) as c:
        r = await c.get("/login", params={"next": "/jobs/abc"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert '<form method="post" action="/login"' in r.text
    assert 'type="password"' in r.text and 'name="api_key"' in r.text
    assert 'name="next" value="/jobs/abc"' in r.text
    assert "Use the API key from your .env (API_KEY)" in r.text
    assert r.headers["content-security-policy"] == CSP


async def test_login_page_escapes_and_sanitises_next(app) -> None:
    async with anon(app) as c:
        r = await c.get("/login", params={"next": '/x"><script>alert(1)</script>'})
    assert "<script>alert" not in r.text
    assert "&#34;&gt;&lt;script&gt;" in r.text or "&quot;&gt;&lt;script&gt;" in r.text
    async with anon(app) as c:
        r = await c.get("/login", params={"next": "//evil.example"})
    assert 'name="next" value="/"' in r.text


async def test_cookie_attributes_http(app) -> None:
    async with anon(app) as c:
        r = await _login(c)
    attrs = [a.strip().lower() for a in r.headers["set-cookie"].split(";")]
    assert attrs[0].startswith(f"{COOKIE}=")
    assert {"httponly", "samesite=strict", "path=/", "max-age=43200"} <= set(attrs)
    assert "secure" not in attrs


async def test_cookie_secure_when_https(app) -> None:
    async with anon(app, base_url="https://research.localhost") as c:
        r = await c.post(
            "/login",
            data={"api_key": TEST_API_KEY, "next": "/"},
            headers={"Origin": "https://research.localhost"},
        )
    assert r.status_code == 303
    attrs = [a.strip().lower() for a in r.headers["set-cookie"].split(";")]
    assert "secure" in attrs


@pytest.mark.parametrize(
    "bad",
    [
        "//evil.example/",
        "/\\evil.example",
        "https://evil.example/",
        "javascript:alert(1)",
        "%2F%2Fevil.example",
        "/%2F%2Fevil.example",
        "/%5Cevil.example",
        "/\t/evil.example",
        "/\n/evil.example",
        "evil",
        "",
        "http:/evil",
        "/x/../\\evil",
    ],
)
async def test_next_must_be_same_site_path(app, bad: str) -> None:
    async with anon(app) as c:
        r = await c.post("/login", data={"api_key": TEST_API_KEY, "next": bad}, headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/"


async def test_next_keeps_safe_path_and_query(app) -> None:
    async with anon(app) as c:
        r = await c.post(
            "/login", data={"api_key": TEST_API_KEY, "next": "/jobs/abc?tab=events"}, headers=ORIGIN
        )
    assert r.headers["location"] == "/jobs/abc?tab=events"


def test_safe_next_unit() -> None:
    assert safe_next("/a/b?c=d") == "/a/b?c=d"
    assert safe_next(None) == "/"
    assert safe_next("/a:b") == "/a:b"  # a colon in the path is not a scheme
    for bad in ("//x", "/\\x", "https://x", "javascript:x", "%2F%2Fx", " /x", "/x\x00"):
        assert safe_next(bad) == "/", bad


# --- Origin checks ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Referer": "http://evil.example/login"},
        {"Origin": "http://research.localhost.evil.example"},
    ],
)
async def test_login_post_requires_same_origin(app, headers: dict[str, str]) -> None:
    async with anon(app) as c:
        r = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"}, headers=headers)
    assert r.status_code == 403
    assert "set-cookie" not in r.headers


async def test_login_post_referer_fallback_and_site_host(app) -> None:
    async with anon(app) as c:
        r = await c.post(
            "/login",
            data={"api_key": TEST_API_KEY, "next": "/"},
            headers={"Referer": "http://research.localhost/login?next=/"},
        )
        assert r.status_code == 303
    # The Host differs (e.g. a port), but the Origin host equals SITE_HOST.
    async with anon(app, base_url="http://research.localhost:8443") as c:
        r = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 303


async def test_cookie_state_change_without_origin_rejected(app) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.post("/v1/search", json={"query": "x", "depth": "quick"})
        assert r.status_code == 403
        body = r.json()
        assert body["data"] is None and body["errors"][0]["code"] == ErrorCode.UNAUTHORIZED
        assert body["errors"][0]["message"] == "cross-origin request refused"
        assert body["meta"]["request_id"] == r.headers["x-request-id"]
        ok = await c.post(
            "/v1/search",
            json={"query": "x", "depth": "quick"},
            headers={"Referer": "http://research.localhost/try"},
        )
        assert ok.status_code == 200
        # Safe methods need no Origin.
        assert (await c.get("/v1/engines")).status_code == 200


async def test_cookie_cross_origin_gui_post_gets_plain_403(app) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.post("/logout", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/plain")
    assert "set-cookie" not in r.headers
    assert r.headers["content-security-policy"] == CSP


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_cookie_other_unsafe_methods_checked(app, method: str) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.request(method, "/v1/jobs/x", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# --- cookie validity -------------------------------------------------------------------------


async def test_expired_cookie_rejected(app) -> None:
    class Old(TimestampSigner):
        def get_timestamp(self) -> int:
            return int(time.time()) - MAX_AGE_S - 5

    old = Old(TEST_ENV["SESSION_SECRET"], salt=SALT).sign(b"gui").decode()
    async with anon(app) as c:
        c.cookies.set(COOKIE, old)
        assert (await c.get("/")).status_code == 303
        assert (await c.post("/v1/search", json={"query": "x"}, headers=ORIGIN)).status_code == 401


async def test_cookie_with_other_payload_or_secret_rejected(app) -> None:
    for value in (
        TimestampSigner(TEST_ENV["SESSION_SECRET"], salt=SALT).sign(b"admin").decode(),
        TimestampSigner("another-secret", salt=SALT).sign(b"gui").decode(),
        TimestampSigner(TEST_ENV["SESSION_SECRET"]).sign(b"gui").decode(),  # default salt
    ):
        async with anon(app) as c:
            c.cookies.set(COOKIE, value)
            assert (await c.get("/")).status_code == 303, value


def test_codec_unit() -> None:
    codec = SessionCodec("s3cret")
    assert codec.valid(codec.issue())
    for bad in (None, "", "gui", "gui.x.y", codec.issue() + "x", "\x00", "é.é.é"):
        assert not codec.valid(bad), bad


@pytest.mark.parametrize(
    "template",
    [
        'a=1; junk; {c}; b="unterminated',
        "re_session=bogus; {c}",
        "{c}; re_session=bogus",
        're_session="{v}"',
        ";;; ===; {c};;",
    ],
)
async def test_cookie_header_parsing_is_robust(app, template: str) -> None:
    value = await _cookie(app)
    header = template.format(c=f"{COOKIE}={value}", v=value)
    async with anon(app) as c:
        r = await c.get("/", headers={"Cookie": header})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "header", ['re_session="', "=;=;=", "re_session", '"; re_session=a"b', "\xff=\xfe"]
)
async def test_malformed_cookie_header_never_crashes(app, header: str) -> None:
    raw = header.encode("latin-1")
    async with anon(app) as c:
        r = await c.get("/", headers={b"Cookie": raw})
        assert r.status_code == 303
        r = await c.post("/v1/search", json={"query": "x"}, headers={b"Cookie": raw})
        assert r.status_code == 401


async def test_websocket_cookie_alone_is_refused(app) -> None:
    value = await _cookie(app)
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(m: dict) -> None:
        sent.append(m)

    scope = {
        "type": "websocket",
        "path": "/ws",
        "headers": [(b"cookie", f"{COOKIE}={value}".encode()), (b"origin", b"http://evil")],
        "query_string": b"",
    }
    await app(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


# --- unauthenticated routing -----------------------------------------------------------------


async def test_unauthenticated_gui_redirect_keeps_path_and_query(app) -> None:
    async with anon(app) as c:
        r = await c.get("/jobs/abc", params={"tab": "events"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/jobs/abc%3Ftab%3Devents"
    assert r.headers["content-security-policy"] == CSP


@pytest.mark.parametrize("path", ["/v1", "/v1/jobs/x", "/mcp", "/mcp/", "/mcp/messages"])
async def test_unauthenticated_api_paths_get_401_envelope(app, path: str) -> None:
    async with anon(app) as c:
        r = await c.post(path, json={})
    assert r.status_code == 401
    assert r.json()["errors"][0]["code"] == ErrorCode.UNAUTHORIZED
    assert "content-security-policy" not in r.headers


async def test_static_is_public(app) -> None:
    async with anon(app) as c:
        r = await c.get("/static/vendor/htmx.min.js")
        assert r.status_code == 200 and r.headers["content-security-policy"] == CSP
        assert (await c.get("/static/app.css")).status_code == 200


# --- rate limiting ---------------------------------------------------------------------------


async def test_rate_limit_blocks_even_the_right_key(app) -> None:
    async with anon(app) as c:
        for _ in range(5):
            assert (await _login(c, "wrong")).status_code == 401
        r = await _login(c)
    assert r.status_code == 429 and "set-cookie" not in r.headers
    assert int(r.headers["retry-after"]) > 0


async def test_rate_limit_is_per_client_ip(app) -> None:
    async with anon(app, ip="10.0.0.1") as c:
        for _ in range(5):
            await _login(c, "wrong")
        assert (await _login(c)).status_code == 429
    async with anon(app, ip="10.0.0.2") as c:
        assert (await _login(c)).status_code == 303


async def test_success_resets_failures(app) -> None:
    async with anon(app) as c:
        codes = [(await _login(c, "wrong")).status_code for _ in range(4)]
        codes.append((await _login(c)).status_code)
        codes += [(await _login(c, "wrong")).status_code for _ in range(4)]
    assert codes == [401] * 4 + [303] + [401] * 4


def test_limiter_window_expires() -> None:
    now = [1000.0]
    lim = LoginRateLimiter(max_failures=5, window_s=60.0, clock=lambda: now[0])
    for _ in range(5):
        assert not lim.blocked("a")
        lim.fail("a")
    assert lim.blocked("a") and not lim.blocked("b")
    assert lim.retry_after("a") == 60
    now[0] += 59.0
    assert lim.blocked("a")
    now[0] += 1.5
    assert not lim.blocked("a")


def test_lockout_is_fixed_from_the_fifth_failure() -> None:
    now = [0.0]
    lim = LoginRateLimiter(max_failures=5, window_s=60.0, clock=lambda: now[0])
    for t in (0.0, 10.0, 20.0, 30.0, 40.0):  # the 5th failure lands at t=40
        now[0] = t
        assert not lim.blocked("a")
        lim.fail("a")
    # A sliding window would unblock at t=60 (when the t=0 failure ages out); the fixed
    # lockout holds until t=40+60.
    for t in (40.0, 60.5, 75.0, 99.9):
        now[0] = t
        assert lim.blocked("a"), t
    now[0] = 99.0
    assert lim.retry_after("a") == 1
    now[0] = 100.0
    assert not lim.blocked("a") and lim.retry_after("a") == 0
    # The slate is clean afterwards: one new failure does not re-block.
    lim.fail("a")
    assert not lim.blocked("a")


def test_failures_outside_window_do_not_accumulate() -> None:
    now = [0.0]
    lim = LoginRateLimiter(max_failures=5, window_s=60.0, clock=lambda: now[0])
    for t in (0.0, 20.0, 40.0, 60.0, 80.0, 100.0):  # never 5 within any 60 s
        now[0] = t
        lim.fail("a")
        assert not lim.blocked("a"), t


def test_limiter_is_bounded() -> None:
    lim = LoginRateLimiter(max_clients=100)
    for i in range(1000):
        lim.fail(f"10.0.{i // 256}.{i % 256}")
        assert not lim.blocked(f"192.168.0.{i % 256}")  # checking never allocates
    assert len(lim) == 100
    lim.reset("nope")


# --- logout and layout -----------------------------------------------------------------------


async def test_logout_clears_cookie_and_redirects(app) -> None:
    async with anon(app) as c:
        await _login(c)
        assert (await c.get("/")).status_code == 200
        r = await c.post("/logout", headers=ORIGIN)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        attrs = [a.strip().lower() for a in r.headers["set-cookie"].split(";")]
        assert attrs[0] in (f'{COOKIE}=""', f"{COOKIE}=") and "max-age=0" in attrs
        assert "path=/" in attrs and "httponly" in attrs
        assert COOKIE not in c.cookies
        assert (await c.get("/")).status_code == 303


async def test_base_layout(app) -> None:
    async with anon(app) as c:
        await _login(c)
        r = await c.get("/")
    html = r.text
    assert r.status_code == 200 and r.headers["content-security-policy"] == CSP
    assert (
        '<meta name="htmx-config" content=\''
        '{"includeIndicatorStyles":false,"allowEval":false,"selfRequestsOnly":true}\'>'
    ) in html
    assert '<link rel="stylesheet" href="/static/app.css">' in html
    for src in ("/static/vendor/htmx.min.js", "/static/vendor/sse.js", "/static/app.js"):
        assert f'<script src="{src}" defer></script>' in html
    assert re.search(r'<form method="post" action="/logout"[^>]*>', html)
    assert 'href="/logout"' not in html


async def test_security_headers_everywhere_but_api(app, client: httpx.AsyncClient) -> None:
    async with anon(app) as c:
        for r in (await c.get("/login"), await c.get("/"), await c.get("/static/app.js")):
            assert r.headers["content-security-policy"] == CSP
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "same-origin"
            assert r.headers["x-frame-options"] == "DENY"
    r = await client.get("/v1/engines")
    assert r.status_code == 200 and "content-security-policy" not in r.headers


def test_templates_have_no_inline_script_or_style() -> None:
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text()
        assert "hx-on" not in text, path
        assert not re.search(r"\sstyle\s*=", text), path
        assert "<style" not in text, path
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", text, re.S):
            attrs, body = m.group(1), m.group(2).strip()
            if re.fullmatch(r'\s+type="application/json"\s+id="[a-z-]+"', attrs):
                # An inert JSON data block (never executed, so CSP-allowed): its only content
                # may be one ``|tojson`` expression, which escapes <, >, & and '.
                assert re.fullmatch(r"\{\{\s*[\w.]+\s*\|\s*tojson\s*\}\}", body), path
                continue
            assert "src=" in attrs and not body, path


def test_no_template_marks_anything_safe() -> None:
    paths = list(TEMPLATES.rglob("*"))
    assert any(p.name == "base.html" for p in paths)
    for path in paths:
        if path.is_file():
            text = path.read_text()
            assert not re.search(r"\|\s*safe\b", text), path
            assert not re.search(r"autoescape\s+false", text, re.I), path


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
async def test_api_docs_disabled(app, client: httpx.AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 404
    assert r.json()["errors"][0]["code"] == ErrorCode.NOT_FOUND
    assert r.headers["content-security-policy"] == CSP
    async with anon(app) as c:  # no longer an open path
        r = await c.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


async def test_openapi_json_stays_open(app) -> None:
    async with anon(app) as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200 and "/v1/search" in r.json()["paths"]
    assert r.headers["content-security-policy"] == CSP


async def test_no_non_api_response_is_csp_exempt(app, client: httpx.AsyncClient) -> None:
    responses = [
        await client.get(p)
        for p in ("/", "/login", "/static/app.css", "/openapi.json", "/health", "/version",
                  "/docs", "/redoc", "/nope")
    ]  # fmt: skip
    async with anon(app) as c:
        responses += [await c.get("/"), await c.get("/docs"), await c.get("/redoc")]
    for r in responses:
        assert r.headers.get("content-security-policy") == CSP, r.request.url


# --- Origin normalisation (scheme, host, effective port) ------------------------------------


@pytest.mark.parametrize(
    ("source", "scheme", "host", "site_host", "ok"),
    [
        ("http://research.localhost", "http", "research.localhost", "research.localhost", True),
        (
            "http://research.localhost:9999",
            "http",
            "research.localhost",
            "research.localhost",
            False,
        ),
        ("https://research.localhost", "http", "research.localhost", "research.localhost", False),
        ("http://research.localhost", "https", "research.localhost", "research.localhost", False),
        ("http://research.localhost:80", "http", "research.localhost", "x.example", True),
        ("http://research.localhost", "http", "research.localhost:80", "x.example", True),
        ("https://research.localhost:443", "https", "research.localhost", "x.example", True),
        ("https://research.localhost:80", "https", "research.localhost", "x.example", False),
        ("HTTP://RESEARCH.LOCALHOST", "http", "Research.Localhost", "x.example", True),
        ("http://[::1]:8080", "http", "[::1]:8080", "x.example", True),
        ("http://[::1]:8081", "http", "[::1]:8080", "x.example", False),
        ("http://[::1]", "http", "[::1]:8080", "x.example", False),
        # site_host: scheme from the request, default port unless site_host carries one
        ("http://research.localhost", "http", "proxy.internal:8443", "research.localhost", True),
        ("https://research.localhost", "https", "proxy.internal", "research.localhost", True),
        ("http://research.localhost:8443", "http", "proxy.internal", "research.localhost", False),
        ("http://research.localhost:8443", "http", "proxy", "research.localhost:8443", True),
        ("http://research.localhost", "http", "proxy", "research.localhost:8443", False),
        # strictness kept
        ("null", "http", "research.localhost", "research.localhost", False),
        ("http://u@research.localhost", "http", "research.localhost", "research.localhost", False),
        ("http://research.localhost.", "http", "research.localhost", "research.localhost", False),
        (
            "http://research.localhost.evil",
            "http",
            "research.localhost",
            "research.localhost",
            False,
        ),
        (
            "http://evil.research.localhost",
            "http",
            "research.localhost",
            "research.localhost",
            False,
        ),
        ("ftp://research.localhost", "http", "research.localhost", "research.localhost", False),
        (
            "http://research.localhost:bad",
            "http",
            "research.localhost",
            "research.localhost",
            False,
        ),
        ("http://[::1", "http", "[::1]", "research.localhost", False),
        ("http://research.localhost", "http", "research.localhost:bad", "x.example", False),
    ],
)
def test_same_origin_normalised(
    source: str, scheme: str, host: str, site_host: str, ok: bool
) -> None:
    assert same_origin([source], [], scheme, host, site_host) is ok
    assert same_origin([], [source + "/page?x=1"], scheme, host, site_host) is ok


def test_same_origin_origin_wins_over_referer_and_all_must_match() -> None:
    good, bad = "http://research.localhost", "http://research.localhost:9999"
    args = ("http", "research.localhost", "research.localhost")
    assert not same_origin([bad], [good + "/"], *args)
    assert not same_origin([good, bad], [], *args)
    assert not same_origin([], [], *args)


@pytest.mark.parametrize(
    "origin",
    ["http://research.localhost:9999", "https://research.localhost", "http://research.localhost."],
)
async def test_cookie_post_with_mismatched_origin_rejected(app, origin: str) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.post(
            "/v1/search", json={"query": "x", "depth": "quick"}, headers={"Origin": origin}
        )
    assert r.status_code == 403


async def test_cookie_post_with_explicit_default_port_allowed(app) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.post(
            "/v1/search",
            json={"query": "x", "depth": "quick"},
            headers={"Origin": "http://research.localhost:80"},
        )
    assert r.status_code == 200


async def test_login_over_ipv6_literal_host(app) -> None:
    async with anon(app, base_url="http://[::1]:8080") as c:
        ok = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"},
                          headers={"Origin": "http://[::1]:8080"})  # fmt: skip
        bad = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"},
                           headers={"Origin": "http://[::1]:9999"})  # fmt: skip
    assert ok.status_code == 303 and bad.status_code == 403


async def test_login_post_scheme_mismatch_rejected(app) -> None:
    async with anon(app, base_url="https://research.localhost") as c:
        r = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"}, headers=ORIGIN)
    assert r.status_code == 403


# --- safe-method allowlist -------------------------------------------------------------------


@pytest.mark.parametrize("method", ["TRACE", "PROPFIND", "MKCOL", "FROB"])
async def test_cookie_non_safe_methods_are_origin_checked(app, method: str) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.request(method, "/v1/search", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403, method
        # /mcp ignores the GUI cookie altogether (key only): 401, not an Origin check.
        r = await c.request(method, "/mcp", headers={"Origin": "http://evil.example"})
        assert r.status_code == 401, method


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_cookie_safe_methods_skip_origin_check(app, method: str) -> None:
    cookie = await _cookie(app)
    async with anon(app) as c:
        c.cookies.set(COOKIE, cookie)
        r = await c.request(method, "/v1/engines", headers={"Origin": "http://evil.example"})
    assert r.status_code != 403 and r.status_code != 401


# --- key comparison never leaks length -------------------------------------------------------


def test_key_matches() -> None:
    expected = b"test-key"
    assert auth_mod.key_matches(b"test-key", expected)
    for bad in (b"", b"t", b"test-kez", b"test-key-longer", b"x" * 10_000):
        assert not auth_mod.key_matches(bad, expected)
    assert auth_mod.key_matches("test-key", expected)  # str form input (the login form)


def test_key_matches_compares_fixed_length_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    for supplied in (b"", b"t", b"test-key", b"x" * 500):
        auth_mod.key_matches(supplied, b"test-key")
    assert seen and all(a == b == 32 for a, b in seen)


async def test_every_key_check_uses_the_helper(app, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []
    real = auth_mod.key_matches

    def spy(supplied: bytes | str, expected: bytes) -> bool:
        calls.append(supplied if isinstance(supplied, bytes) else supplied.encode())
        return real(supplied, expected)

    monkeypatch.setattr(auth_mod, "key_matches", spy)
    async with anon(app) as c:
        await c.post("/v1/search", json={"query": "x"}, headers={"X-API-Key": "k1"})
        await c.post("/login", data={"api_key": "k2", "next": "/"}, headers=ORIGIN)

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(m: dict) -> None:
        pass

    await app(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"x-api-key", b"k3")],
            "query_string": b"",
        },
        receive,
        send,
    )
    assert {b"k1", b"k2", b"k3"} <= set(calls)
