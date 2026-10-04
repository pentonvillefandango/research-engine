# ADR-0022: Chromium sandbox stays on in Crawl4AI

## Status

Accepted (2026-10-04). Source: B3.

## Context

REQUIREMENTS §12 forbids `--no-sandbox`. Crawl4AI 0.9.4's default config includes `--no-sandbox`, and it can be disabled with `CRAWL4AI_CHROMIUM_SANDBOX=true`. Chromium's namespace sandbox then needs user namespaces, which Docker's default seccomp/AppArmor profiles may restrict. The host kernel allows unprivileged user namespaces (`user.max_user_namespaces=63651`, `kernel.unprivileged_userns_clone=1`, checked 2026-10-04).

## Decision

Run Crawl4AI with the sandbox enabled. Build step 3 (Task 3.0) verifies it under Docker's default profiles. If that fails, ship a minimal per-service seccomp profile (never `privileged`, never `SYS_ADMIN`). If that also fails, stop and ask the owner. `scripts/check_sandbox.sh` is the evidence command.

## Consequences

Stronger isolation for untrusted pages. A custom seccomp profile, if one is needed, must be maintained alongside Crawl4AI upgrades.

## Outcome

Filled in by build step 3, Task 3.0: the path taken, the verbatim `scripts/check_sandbox.sh` output, and the date.
