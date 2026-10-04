# ADR-0016: VM resources are not a constraint; per-service limits still apply

## Status

Accepted (2026-10-04). Source: D16.

## Context

There's spare CPU and RAM on the Proxmox host, but the VM is shared.

## Decision

Grow the VM as needed, but give every service memory and CPU limits, configurable via `.env`.

## Consequences

A runaway browser can't starve the other tools. The limits are a policy test in `tests/test_compose_policy.py`.
