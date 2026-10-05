# ADR-0026: SSRF guard and robots.txt semantics

## Status

Accepted (2026-10-04). Source: design addendum §5.

## Context

The service fetches arbitrary URLs supplied by agents, which could target internal services. Politeness requires honouring robots.txt.

## Decision

The SSRF guard resolves the host and rejects loopback, private, link-local (including cloud metadata), CGNAT, multicast, reserved and unspecified addresses (including IPv4-mapped IPv6), plus `localhost` names. For static fetches it runs before the request and on every redirect hop. For browser fetches (Crawl4AI) it checks the start URL, which is sent to Crawl4AI exactly as checked, and the final URL; Chromium's intermediate redirects and the page's subresources are not checked by this guard (see Consequences). `SSRF_ALLOW_HOSTS` exempts named hosts. robots.txt follows RFC 9309: a 4xx response means allow all; a 5xx or network error means disallow all, cached for 10 minutes; a redirect is treated as allow in V1. Crawl-delay raises the per-domain delay, capped at 30 s.

## Consequences

Residual risk: DNS can change between the check and the connection (rebinding). This is mitigated by the per-hop checks and by Crawl4AI's own internal-URL block, and accepted for V1. **Browser path gap:** Chromium follows redirects and loads subresources (images, scripts, frames, XHR) itself, so a public page can make the browser request an internal address that this guard never sees. Only the start and final URLs are checked here (plus robots on the final URL); the rest relies on Crawl4AI's own guard and the sandbox. Blocking the `crawl4ai` container's LAN egress at the network layer is the planned V2 fix (ADR-0022, Deferred). A future option is to pin the connection to the resolved IP. The app trusts forwarded headers from any source (`--forwarded-allow-ips '*'`), which is acceptable only because it publishes no port and only Caddy reaches it (but see the co-tenancy caveat in the implementation notes).

## Implementation notes

- **One parser.** The guard parses with `httpx.URL` (IDNA-2008) and checks `raw_host`, the exact host httpx connects to. An earlier version used Python's IDNA-2003 codec, so `straße.example` was checked as `strasse.example` while httpx connected to `xn--strae-oqa.example`. `SsrfGuard.check` now returns the parsed, userinfo-free `httpx.URL`, and `StaticFetcher` and `RobotsPolicy` request exactly that object. Redirects are resolved with `httpx.URL.join`. The allow-list is normalised the same way. Invalid IDNA (for example a ZWJ) is blocked.
- **Legacy numeric hosts** (`127.1`, `0x7f.1`, `2130706433`, `0`) are canonicalised with `inet_aton` inside the guard, so safety never depends on the resolver. Also blocked: `fec0::/10` and `64:ff9b:1::/48`, plus NAT64 `64:ff9b::/96` mapped to its embedded IPv4.
- **Cookie-rejecting fetch client.** `make_fetch_client()` (`safety/http.py`) returns a client with a cookie policy that accepts no domains, `follow_redirects=False` and `trust_env=False`. Requests are built bare, so no cookies, auth or client default headers reach the target. Userinfo in a URL is stripped and never becomes Basic auth.
- **429 is not retried** inside `fetch`. It is `fetch_failed` with `retryable=true`, so the caller may retry later. 5xx and network errors are retried (3 attempts), all inside one `timeout_s` budget; expiry raises `upstream_timeout`.
- **Unchanged:** the DNS-rebinding window between check and connect remains.
- **robots.txt fetch and the page budget (v1.0.1).** The robots.txt fetch has its own 15 s timeout, but it runs inside the page fetch's budget, `min(timeout_s, PAGE_TIMEOUT_S)`. If that budget is shorter than the robots.txt fetch takes, the page deadline cancels it: nothing is cached, and each fetch to that origin refetches robots.txt and ends in `upstream_timeout` (retryable). That is accepted and documented in the README's `PAGE_TIMEOUT_S` row (keep it at 15 s or more). Letting the robots.txt fetch outlive its caller, so it could finish and be cached, would need a shared in-flight task per origin in place of the per-origin lock, plus shutdown handling for detached tasks. That is too much change for a patch release, and it would let work run past the operator's per-page cap.
- **Container proxy-header trust.** The app image runs uvicorn with `--forwarded-allow-ips "*"`. This is acceptable because `app` publishes no port: only Caddy (on the `proxy` network) and internal services can reach it, so `X-Forwarded-*` headers cannot be spoofed from outside. **Caveat (co-tenancy, step 8):** "only Caddy" is an approximation. The `proxy` network is VM-wide and shared, so any other co-tenant container on it can reach `research-engine-app:8000` directly. That bypasses Caddy's `LAB_SUBNET` gate, and such a container can send any `X-Forwarded-For`, which uvicorn then trusts. Two things are affected: the GUI login rate limiter, which is keyed on the client IP (`gui/routes.py:_client_ip`), so a co-tenant could rotate the key or throttle another IP; and the client IP in logs and events. **Accepted for V1** because every route still requires the API key or a session, and the co-tenants are the owner's own tools. Restricting `--forwarded-allow-ips` to Caddy's address would need a fixed subnet on `proxy`. That is a VM-wide choice, outside this project, so it's left for V2. Rate limiting that doesn't rely on the forwarded IP alone is the other V2 option.

## Caller-facing behaviour: what callers see (step 9, revised v1.0.1)

Documented for agents in `docs/USING.md` and in the README's security notes:

- Both refusals are HTTP 403: `ssrf_blocked` and `robots_disallowed`. Over MCP the tool error text is `<code>: <message> (retryable=true|false)`. `ssrf_blocked` and a real robots.txt `Disallow` match are `retryable: false`: a caller should choose another source rather than retry.
- There is no per-request override for either. `SSRF_ALLOW_HOSTS` is the operator's only exemption, and robots.txt has none.
- The robots.txt user-agent token is the first word of `USER_AGENT` (`ResearchEngine` by default). Crawl-delay raises that domain's pacing for every caller, up to 30 s, on top of `DOMAIN_CONCURRENCY` and `DOMAIN_DELAY_S`.
- A robots.txt that can't be fetched (5xx, network error or timeout) blocks the whole origin for 10 minutes, as RFC 9309 requires. Callers see `robots_disallowed` for that period, even if the site has no robots rules, but **with `retryable: true`** (since v1.0.1) and a message saying robots.txt was unavailable, with the HTTP status (`HTTP 503`) or the network error (`network error: ConnectError`, `network error: timeout`), and to retry later. No new error code: the `ErrorCode` enum is unchanged.
- **HTTP status stays 403 for both cases** (v1.0.1 decision). The refusal is this service's policy decision under RFC 9309, not a failure of the service or of the page itself, so a 5xx would misreport it, and a 503 would also prompt generic HTTP clients to retry immediately, while the answer can't change until the 10-minute cache entry expires. `retryable` in the error body is the signal that waiting may help.
