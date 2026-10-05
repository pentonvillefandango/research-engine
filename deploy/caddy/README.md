# Shared Caddy project

Tool-agnostic reverse proxy for the VM. It owns host ports 80/443 and creates the `proxy`
network that tools join. Each tool adds one file in `sites/`. Hostnames in this repo are
examples; real values live in `.env`.

## Install

```sh
mkdir -p /opt/caddy && cp -r deploy/caddy/. /opt/caddy/ && cd /opt/caddy
cp .env.example .env && chmod 600 .env   # edit SITE_HOST, LAB_SUBNET, TOOLBOX_HOST
docker compose up -d
```

Caddy must start first: it creates the `proxy` network (owned by the `caddy` project), which
tools join as an external network. `SITE_HOST` here must match the tool's own `.env`
(for research-engine, its `SITE_HOST`), or `/mcp` returns 421.

Changing `.env` needs `docker compose up -d --force-recreate`; `caddy reload` does not re-read
the container environment. The `Caddyfile` is a single-file bind mount, so an editor or rsync
that replaces the file leaves the container on the old inode: recreate the container after
editing it. Edits under `sites/` plus a reload are fine (directory mount).

Back up the Caddy root CA (private key in the `caddy-data` volume, under
`/data/caddy/pki/authorities/local/`). Losing it means a new CA and every client must re-trust.

`SITE_HOST` and `LAB_SUBNET` are both required. If either is empty the site fails closed
(no lab client matches, so everything gets 403). The `TOOLBOX_HOST` index page is lab-only
too: it has the same `@lab remote_ip {$LAB_SUBNET}` gate, and every other client gets 403. `LAB_SUBNET` takes one or more
space-separated CIDRs. Keep `.env` mode 600.

Validate without starting anything (no ports, removed afterwards):

```sh
docker run --rm -e SITE_HOST=research.localhost -e LAB_SUBNET=127.0.0.1/32 \
  -e TOOLBOX_HOST=toolbox.localhost -v "$PWD":/etc/caddy:ro caddy:2.11.6 \
  caddy validate --config /etc/caddy/Caddyfile
```

## Adding a tool

Drop `sites/<tool>.caddy` (join the `proxy` network with a stable, tool-prefixed alias such as
`research-engine-app`; never reference a bare service name like `app`, which other tools may
share), then:

```sh
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Host-side checks: from the VM itself, requests via 127.0.0.1 get 403 (the Docker bridge source is
outside `LAB_SUBNET`). Use the VM's lab IP with `curl --resolve <SITE_HOST>:443:<lab-ip>`.

## Forwarded headers and trust

Caddy forwards `Host` and `X-Forwarded-Proto` (and the other `X-Forwarded-*` headers) from the
real client, so the app's Origin check sees `https://<SITE_HOST>`. Never set `trusted_proxies`
here: that would make Caddy trust client-supplied forwarded headers.

Caveat (ADR-0026): any co-tenant container on `proxy` can reach the apps directly, bypassing
the lab-subnet restriction in the site files and the index page. The application's API key still applies.

## Serve at the site root, never under a path prefix

Serve the app at the root of its own hostname, as the site file does. Do not mount it under a
path prefix (`handle_path /research/*`, `uri strip_prefix`) and never start uvicorn with
`--root-path`. The auth middleware decides what is open (`/health`, `/version`, `/login`, `/static/` ...),
API-key-only (`/mcp`) or cookie-or-key (`/v1`) from the raw request path, and the GUI's Origin
check and redirects assume the app owns `/`. Under a prefix those assumptions break.

## Plain-HTTP alternative

If you do not want to trust a local CA, serve plain HTTP instead: in the site file use
`http://{$SITE_HOST}` as the site address and drop `tls internal` (see the commented block in
`sites/research-engine.caddy`).

## Trusting Caddy's root CA (macOS)

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root.crt
```
