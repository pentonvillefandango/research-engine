# Research Engine

## What it is

A self-hosted web search and scraping service that research agents call over REST and MCP. It returns typed, provenance-tagged structured data. Research quality comes before speed. The reasoning stays in the calling agent; this service finds, fetches, cleans and structures.

## Status

V1 in development.

## Quick start

Docker Compose quick start lands in build step 8.

For development:

```bash
uv sync && uv run pytest
```

The default site host is `research.localhost`.

## Licence

MIT. See [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.
