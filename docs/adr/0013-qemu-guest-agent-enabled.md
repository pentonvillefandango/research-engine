# ADR-0013: QEMU guest agent enabled

## Status

Accepted (2026-10-04). Source: D13.

## Context

Proxmox operations need clean shutdowns, snapshots and IP reporting.

## Decision

Enable the QEMU guest agent in Proxmox and install it in the guest.

## Consequences

Clean lifecycle operations. It's documented in `docs/deploy-toolbox.md`.
