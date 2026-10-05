# Examples

Two runnable scripts. Run them from the repository root after `uv sync`.

| Variable | Used by | Meaning |
| --- | --- | --- |
| `RESEARCH_ENGINE_URL` | both | Base URL of the service. Default `https://research.localhost`. For a deployment, e.g. `https://research.toolbox.home.arpa`. |
| `RESEARCH_ENGINE_API_KEY` | both | The service's API key (sent as `X-API-Key`). Never printed. |
| `OPENAI_API_KEY` | agent example | Your own OpenAI key. |
| `OPENAI_MODEL` | agent example | Optional. When unset, the Agents SDK picks its default model. |

## `client_usage.py`

Uses the typed `ResearchEngineClient`: runs a search, fetches the top result, and prints the
title, the word count and the provenance (URL, fetch time, method, content hash).

```bash
export RESEARCH_ENGINE_URL=https://research.toolbox.home.arpa
export RESEARCH_ENGINE_API_KEY=...
uv run python examples/client_usage.py
```

A failure (HTTP error, timeout, connection refused) prints `error: ...` to stderr and exits
non-zero. The message never contains the key.

## `openai_agents_mcp.py`

An OpenAI Agents SDK agent (`researcher`) connects to the service's MCP endpoint (`/mcp`,
streamable HTTP, `X-API-Key` header), uses `web_search`, `web_fetch` and `search_and_read`,
and answers with cited source URLs. Its instructions tell it to treat fetched content as
untrusted. It prints the final answer and then the names of the tool calls it made (not the raw
tool results, which can be large).

```bash
export OPENAI_API_KEY=...        # your own key
uv run python examples/openai_agents_mcp.py
```

`OPENAI_API_KEY` is yours and is used only by this example, to call OpenAI from your machine.
The service itself makes no model calls. If `OPENAI_API_KEY` is unset the script prints
`skipped: OPENAI_API_KEY not set` and exits 0.

## TLS: trusting Caddy's internal CA

The stack serves HTTPS with Caddy's `tls internal`, so its certificate is signed by Caddy's own
root CA, which clients must trust or they fail with `CERTIFICATE_VERIFY_FAILED`. Export the root
certificate from Caddy (`root.crt`, in Caddy's data directory under `pki/authorities/local/`) and
point `SSL_CERT_FILE` at it:

```bash
export SSL_CERT_FILE=/path/to/root.crt
```

This works for both examples: the typed client uses `httpx`, and the Agents SDK's MCP client
uses `httpx2`, and both read `SSL_CERT_FILE`. Note that the variable replaces the default CA
bundle for those processes, which is fine here because they only talk to the service. Installing
the root in the operating system trust store is not enough on its own: `httpx2` would use it,
but `httpx` (and so `client_usage.py`) ships its own CA bundle and ignores it.

## Privacy

The OpenAI Agents SDK uploads run traces to OpenAI by default, and a trace can include the
content of the pages the agent fetched. To turn that off, set `OPENAI_AGENTS_DISABLE_TRACING=1`
before running `openai_agents_mcp.py`.
