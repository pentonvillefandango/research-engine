# ADR-0017: Hostname `toolbox`; tools as subdomains via Caddy

## Status

Accepted (2026-10-04). Source: D17.

## Context

Tools need memorable, stable addresses on the lab network.

## Decision

The VM is called `toolbox`, and each tool gets a subdomain (for example `research.toolbox`). UniFi DNS has one A record for `toolbox` plus a CNAME per tool.

## Consequences

Adding a tool means one CNAME and one Caddy site file. Hostnames come from `.env` (`SITE_HOST`) and are never hard-coded (ADR-0020).
