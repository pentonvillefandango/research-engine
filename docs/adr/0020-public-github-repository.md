# ADR-0020: Public GitHub repository

## Status

Accepted (2026-10-04). Source: D20.

## Context

The repo is public and meant to be shared and reused.

## Decision

Never commit secrets or lab-specific values. Hostnames, IPs, paths and usernames come from `.env` with neutral defaults. gitleaks runs in pre-commit and CI, GitHub secret scanning with push protection is on, and Dependabot is enabled. The docs are written for outsiders; `toolbox` is documented only as an example deployment.

## Consequences

There's extra discipline on every commit. Tests assert that no IPv4 literals or `/home/` paths appear in committed docs or Caddy config.
