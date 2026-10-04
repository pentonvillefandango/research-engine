# ADR-0015: Shared Caddy on the tools VM, config shipped in this repo

## Status

Accepted (2026-10-04). Source: D15.

## Context

There's no reverse proxy in the lab yet, and every tool on the VM needs HTTPS by hostname.

## Decision

Ship a tool-agnostic Caddy Compose project in `deploy/caddy/`, installed once at `/opt/caddy/`. It owns ports 80/443 and the external `proxy` network, and imports `sites/*.caddy`.

## Consequences

One proxy serves every tool. Each tool adds one site file. Only this stack's `app` service joins `proxy`.
