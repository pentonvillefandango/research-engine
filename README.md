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

## MCP

The MCP server is at `https://<SITE_HOST>/mcp` (streamable HTTP, stateless, JSON responses). Send the API key in the `X-API-Key` header; the GUI session cookie is not accepted here. Only POST is served. `/mcp/` works the same way; neither path redirects. It has four tools, each with an output schema and structured content: `web_search`, `web_fetch`, `search_and_read` and `get_job`. The server rejects a `Host` header outside `SITE_HOST`, `localhost`, `127.0.0.1` and `research.localhost`.

## Licence

MIT. See [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.
