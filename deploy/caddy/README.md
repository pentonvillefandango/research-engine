# Shared Caddy project

Tool-agnostic reverse proxy for the VM. It owns host ports 80/443 and creates the `proxy`
network that tools join. Each tool adds one file in `sites/`. Hostnames in this repo are
examples; real values live in `.env`.

## Install

```sh
sudo cp -r deploy/caddy /opt/caddy && cd /opt/caddy
cp .env.example .env     # edit SITE_HOST, LAB_SUBNET, TOOLBOX_HOST
docker compose up -d
```

Validate without starting anything (no ports, removed afterwards):

```sh
docker run --rm -e SITE_HOST=research.localhost -e LAB_SUBNET=127.0.0.1/32 \
  -e TOOLBOX_HOST=toolbox.localhost -v "$PWD":/etc/caddy:ro caddy:2.11.6 \
  caddy validate --config /etc/caddy/Caddyfile
```

## Adding a tool

Drop `sites/<tool>.caddy` (join the `proxy` network with a stable alias), then:

```sh
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Forwarded headers and trust

Caddy forwards `Host` and `X-Forwarded-Proto` (and the other `X-Forwarded-*` headers) from the
real client, so the app's Origin check sees `https://<SITE_HOST>`. Never set `trusted_proxies`
here: that would make Caddy trust client-supplied forwarded headers.

Caveat (ADR-0026): any co-tenant container on `proxy` can reach the apps directly, bypassing
the lab-subnet restriction in the site file. The application's API key still applies.

## Plain-HTTP alternative

If you do not want to trust a local CA, serve plain HTTP instead: in the site file use
`http://{$SITE_HOST}` as the site address and drop `tls internal` (see the commented block in
`sites/research-engine.caddy`).

## Trusting Caddy's root CA (macOS)

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root.crt
```
