# ADR-0018: One environment: develop and run on `toolbox`

## Status

Accepted (2026-10-04). Source: D18.

## Context

A separate dev or staging stack would add cost and complexity for a single-owner lab service.

## Decision

Develop and run on the same VM. The safety net is process: commit before going live, `make deploy` with a smoke test and automatic rollback, and unit tests that never touch the live stack.

## Consequences

It's simple. The live stack always runs a known commit (deploys run from a git worktree of that commit, see ADR-0024). A dev Compose override with ephemeral loopback ports supports integration testing without touching the live stack's networking.
