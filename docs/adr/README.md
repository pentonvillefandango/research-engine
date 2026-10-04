# Architecture Decision Records

Seeded from REQUIREMENTS.md §2 (D1–D20) and the 2026-10-04 brainstorming (design addendum `docs/superpowers/specs/2026-10-04-v1-design.md`).

| ADR | Title | Source |
|---|---|---|
| [0001](0001-build-on-searxng-and-crawl4ai-behind.md) | Build on SearXNG and Crawl4AI behind our own FastAPI service | D1 |
| [0002](0002-do-not-self-host-firecrawl.md) | Do not self-host Firecrawl | D2 |
| [0003](0003-no-paid-search-or-scraping-apis.md) | No paid search or scraping APIs | D3 |
| [0004](0004-model-spend-via-a-litellm-model.md) | Model spend via a LiteLLM model selector with per-task routing and a budget cap | D4 |
| [0005](0005-v1-makes-no-model-calls.md) | V1 makes no model calls | D5 |
| [0006](0006-uv-based-python-3-12-workspace.md) | uv-based Python 3.12+ workspace with src layout | D6 |
| [0007](0007-gui-is-fastapi-htmx-server-sent.md) | GUI is FastAPI + HTMX + server-sent events | D7 |
| [0008](0008-v1-gui-includes-a-test-console.md) | V1 GUI includes a test console that calls the public API | D8 |
| [0009](0009-sqlite-wal-for-jobs-events-cache.md) | SQLite (WAL) for jobs, events, cache and results in V1 | D9 |
| [0010](0010-vpn-egress-via-gluetun-http-proxy.md) | VPN egress via Gluetun HTTP-proxy profiles, V2, optional | D10 |
| [0011](0011-deploy-as-one-docker-compose-project.md) | Deploy as one Docker Compose project | D11 |
| [0012](0012-run-on-a-debian-vm-not.md) | Run on a Debian VM, not an LXC | D12 |
| [0013](0013-qemu-guest-agent-enabled.md) | QEMU guest agent enabled | D13 |
| [0014](0014-agpl-components-run-as-separate-containers.md) | AGPL components run as separate containers called over HTTP only | D14 |
| [0015](0015-shared-caddy-on-the-tools-vm.md) | Shared Caddy on the tools VM, config shipped in this repo | D15 |
| [0016](0016-vm-resources-are-not-a-constraint.md) | VM resources are not a constraint; per-service limits still apply | D16 |
| [0017](0017-hostname-toolbox-tools-as-subdomains-via.md) | Hostname `toolbox`; tools as subdomains via Caddy | D17 |
| [0018](0018-one-environment-develop-and-run-on.md) | One environment: develop and run on `toolbox` | D18 |
| [0019](0019-claude-code-operates-the-live-stack.md) | Claude Code operates the live stack through scripted ops commands | D19 |
| [0020](0020-public-github-repository.md) | Public GitHub repository | D20 |
| [0021](0021-gui-authentication-with-a-session-cookie.md) | GUI authentication with a session cookie | B2 |
| [0022](0022-chromium-sandbox.md) | Chromium sandbox stays on in Crawl4AI | B3 |
| [0023](0023-httpx-in-our-code-httpx2-only.md) | httpx in our code; httpx2 only inside the MCP SDK | checked 2026-10-04 |
| [0024](0024-git-workflow-and-deploy-from-worktree.md) | Git workflow and deploy-from-worktree | B5 |
| [0025](0025-build-order-adjustments-for-v1.md) | Build-order adjustments for V1 | design addendum §4 |
| [0026](0026-ssrf-and-robots-semantics.md) | SSRF guard and robots.txt semantics | design addendum §5 |
