# ADR-0017: Hostname `toolbox`; tools as subdomains via Caddy

## Status

Accepted (2026-10-04). Source: D17.

## Context

Tools need memorable, stable addresses on the lab network.

## Decision

The VM is called `toolbox`, and each tool gets a subdomain (for example `research.toolbox`). UniFi DNS has one A record for `toolbox` plus a CNAME per tool.

## Consequences

Adding a tool means one CNAME and one Caddy site file. Hostnames come from `.env` (`SITE_HOST`) and are never hard-coded (ADR-0020).

## Amendment (2026-10-05)

The owner configured UniFi local DNS under the special-use domain `home.arpa` (RFC 8375), which is the standard suffix for home networks and avoids clashing with real TLDs. The actual names are:

- **Host (A):** `toolbox.home.arpa` → the VM's fixed IP.
- **Alias (CNAME):** `research.toolbox.home.arpa` → `toolbox.home.arpa`.

This deployment sets `SITE_HOST=research.toolbox.home.arpa` in `.env`, and the Caddy index host is `toolbox.home.arpa`. Each future tool needs one more CNAME (`<tool>.toolbox.home.arpa` → `toolbox.home.arpa`) plus a Caddy site file. REQUIREMENTS D17 and §9 still say `research.toolbox`; this amendment supersedes them for the example deployment. Code keeps the neutral default `research.localhost`.
