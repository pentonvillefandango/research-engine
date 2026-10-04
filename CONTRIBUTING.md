# Contributing

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Docker

## Setup

```bash
uv sync
uv run pre-commit install
```

## Checks

Run all three before every commit:

```bash
uv run ruff check
uv run pyright
uv run pytest
```

## Conventions

- Test-driven development is expected: write the failing test first.
- Use conventional commits that include the feature IDs, e.g. `feat(search): SearXNG adapter (V1-01)`.
- Never commit `.env` or any secret. The gitleaks pre-commit hook and CI will reject it.
