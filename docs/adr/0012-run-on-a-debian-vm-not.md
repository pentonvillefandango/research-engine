# ADR-0012: Run on a Debian VM, not an LXC

## Status

Accepted (2026-10-04). Source: D12.

## Context

The service loads untrusted pages in a headless browser.

## Decision

Run on a Debian VM (`toolbox`) on Proxmox, not in an LXC container.

## Consequences

The VM has its own kernel and better isolation. Chromium's sandbox can work without `--no-sandbox` (ADR-0022), and Gluetun's TUN device works without passthrough. Other tools on the VM share its kernel, so this stack must be a good co-tenant (REQUIREMENTS §9).
