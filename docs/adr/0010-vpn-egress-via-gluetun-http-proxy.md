# ADR-0010: VPN egress via Gluetun HTTP-proxy profiles, V2, optional

## Status

Accepted (2026-10-04). Source: D10.

## Context

Region-specific results and keeping research traffic off the home IP are useful, but VPN exits attract more CAPTCHAs.

## Decision

In V2, offer named egress profiles: `direct` (the default) or Gluetun containers exposing HTTP proxies, selected per request. Never use them to get round blocks, logins or CAPTCHAs.

## Consequences

Not part of V1; Compose carries only a commented profile stub. Note: Crawl4AI 0.9.x rejects per-request proxy settings and only accepts server-side upstream proxies, so V2 will need one Crawl4AI instance per egress profile or an equivalent design (recorded on 2026-10-04 while checking current APIs).
