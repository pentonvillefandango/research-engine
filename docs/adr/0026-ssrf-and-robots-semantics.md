# ADR-0026: SSRF guard and robots.txt semantics

## Status

Accepted (2026-10-04). Source: design addendum §5.

## Context

The service fetches arbitrary URLs supplied by agents, which could target internal services. Politeness requires honouring robots.txt.

## Decision

The SSRF guard resolves the host and rejects loopback, private, link-local (including cloud metadata), CGNAT, multicast, reserved and unspecified addresses (including IPv4-mapped IPv6), plus `localhost` names. It runs before the request and on every redirect hop. `SSRF_ALLOW_HOSTS` exempts named hosts. robots.txt follows RFC 9309: a 4xx response means allow all; a 5xx or network error means disallow all, cached for 10 minutes; a redirect is treated as allow in V1. Crawl-delay raises the per-domain delay, capped at 30 s.

## Consequences

Residual risk: DNS can change between the check and the connection (rebinding). This is mitigated by the per-hop checks and by Crawl4AI's own internal-URL block, and accepted for V1. A future option is to pin the connection to the resolved IP. The app trusts forwarded headers from any source (`--forwarded-allow-ips '*'`), which is acceptable only because it publishes no port and only Caddy reaches it.
