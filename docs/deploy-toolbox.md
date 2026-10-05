# Example deployment: the `toolbox` VM

This is one worked example, not a requirement. It runs Research Engine on a Debian VM named `toolbox` on Proxmox, behind a UniFi router, at `https://research.toolbox.home.arpa`. Replace the names and addresses with your own. Day-to-day operation is in [OPERATIONS.md](OPERATIONS.md).

## 1. Create the VM (Proxmox)

- **OS:** Debian (stable), with no desktop.
- **Size:** start at **8 vCPU and 16 GB RAM**. This stack's own budget is about 4 vCPU and 8 GB, mostly for the headless browser; the rest is for other tools on the same VM. Give the disk room for Docker images, backups and logs. Grow the VM as tools are added.
- **Why a VM, not an LXC:** the service loads untrusted pages in Chromium. A VM has its own kernel and lets Chromium's sandbox run normally (ADR-0012).
- **QEMU guest agent:** in Proxmox, turn on VM → Options → QEMU Guest Agent → Use QEMU Guest Agent. Then, in the guest:

  ```bash
  sudo apt update && sudo apt install qemu-guest-agent
  sudo systemctl start qemu-guest-agent
  ```

  A "static unit" message is normal on Debian: it starts at boot. If you only just turned the option on, do a full Shutdown and Start from Proxmox. Check it on the VM's Summary page (it shows the IPs) or with `qm agent <vmid> ping` on the Proxmox host.
- **Snapshots:** take a Proxmox snapshot before VM-level changes such as OS or Docker upgrades.

## 2. Fix the VM's IP address

In UniFi Network, open the VM's client entry and set a **fixed IP** (a DHCP reservation). Below, `<vm-lab-ip>` means this address, and `<lab-cidr>` means the lab network that may use the service, in CIDR form.

## 3. Install Docker and the tools

Follow Docker's guide for Debian (docs.docker.com, "Install Docker Engine on Debian"), using its apt repository:

```bash
sudo apt install -y ca-certificates curl git make openssl python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" |
  sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"    # then log out and back in
```

Membership of the `docker` group is effectively root on the VM. That is accepted for a home lab (ADR-0019).

## 4. Get the code

```bash
sudo install -d -o "$USER" /opt/research-engine /opt/caddy
git clone https://github.com/pentonvillefandango/research-engine.git /opt/research-engine
cd /opt/research-engine
```

## 5. Create `.env` and run `make bootstrap`

`SITE_HOST` comes from the repository's `.env`, the single source, so set it **before** the first bootstrap. Then one run is enough. Bootstrap keeps an existing `.env`, so generate its secrets yourself:

```bash
cp .env.example .env && chmod 600 .env
sed -i 's/^SITE_HOST=.*/SITE_HOST=research.toolbox.home.arpa/' .env
for k in API_KEY SESSION_SECRET SEARXNG_SECRET CRAWL4AI_API_TOKEN; do
  sed -i "s/^$k=.*/$k=$(openssl rand -hex 32)/" .env
done
```

Bootstrap installs the shared Caddy in `/opt/caddy` (which creates the `proxy` network), writes `SITE_HOST` into `/opt/caddy/.env`, and turns on the nightly backup timer. It uses `sudo` for the timer, so run it yourself. Preview it first with `ops/bootstrap.sh --dry-run`.

```bash
LAB_SUBNET='<lab-cidr>' make bootstrap
```

- `LAB_SUBNET` is needed only the first time, when `/opt/caddy/.env` doesn't exist yet. It takes one or more space-separated CIDRs. Clients outside it get 403. `TOOLBOX_HOST=toolbox.home.arpa` is optional (the host name of the index page).
- If you skip the `.env` step, bootstrap creates `.env` with generated secrets and the placeholder `research.localhost`. Then set `SITE_HOST` in `.env` and run `make bootstrap` again. The placeholder never replaces a real value already in `/opt/caddy/.env`.

The two `SITE_HOST` values must stay equal. If they drift, `/mcp` returns 421, and `make health` reports `site_host_match: false`.

## 6. Add the DNS records (UniFi)

UniFi doesn't document wildcard records, so use one A record for the VM and one CNAME per tool:

| Type | Name | Points to |
| --- | --- | --- |
| Host (A) | `toolbox.home.arpa` | `<vm-lab-ip>` |
| Alias (CNAME) | `research.toolbox.home.arpa` | `toolbox.home.arpa` |

Where to add them:
- **UniFi Network 9.4:** Settings → Policy Table → Create New Policy → DNS.
- **UniFi Network 9.3:** Settings → Policy Engine → DNS.

A new tool later needs one more CNAME and one Caddy site file. If the VM's IP changes, only the A record changes.

## 7. Trust Caddy's root CA (macOS)

Caddy serves HTTPS with its own private CA (`tls internal`). Copy the root certificate to the Mac and trust it:

```bash
# on the VM
cd /opt/caddy && docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .
# on the Mac, after copying root.crt across (for example with scp)
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root.crt
```

Python clients also need it in an `SSL_CERT_FILE` bundle that keeps the public CAs: see [examples/README.md](../examples/README.md#tls-trusting-caddys-internal-ca). Back up the CA itself (the `caddy-data` volume), as described in [OPERATIONS.md](OPERATIONS.md#backup-and-restore).

## 8. Deploy

Commit any change first: `make deploy` refuses a dirty tree.

```bash
make deploy
```

It builds the app from the current commit, starts the stack, runs the smoke test and the sandbox check, and rolls back by itself if either fails.

## 9. Verify

On the VM:

```bash
make status && make health && make smoke && make sandbox
curl --cacert /opt/caddy/root.crt --resolve "research.toolbox.home.arpa:443:<vm-lab-ip>" \
  https://research.toolbox.home.arpa/health
```

From the Mac:

- `https://toolbox.home.arpa/` shows the index of tools.
- `https://research.toolbox.home.arpa/` shows the login page. Log in with `API_KEY` from `.env` (read it on the VM; never paste it anywhere shared).
- The test console (`/try`) runs the demo set.
- An MCP client configured as in [USING.md](USING.md#minimal-mcp-client-config) lists four tools.

## Troubleshooting

- **403 from the VM itself.** Requests through `127.0.0.1` get 403 by design: Caddy sees the Docker bridge address, which is outside `LAB_SUBNET`. Use the lab IP with `--resolve`, as above.
- **403 from a lab machine.** Its address isn't in `LAB_SUBNET`. Fix `/opt/caddy/.env`, then `docker compose up -d --force-recreate` in `/opt/caddy`.
- **The browser can't find the host, but `dig research.toolbox.home.arpa` works.** Browsers that use DNS-over-HTTPS (for example Chrome's "Use secure DNS" with Cloudflare) skip the router, so they can't resolve `*.home.arpa`. Turn secure DNS off, or set it to use your current DNS provider.
- **Certificate warnings.** The root CA isn't trusted yet (step 7), or Caddy made a new CA after losing its data volume. Trust the new `root.crt`.
- **`/mcp` returns 421.** `SITE_HOST` differs between `/opt/research-engine/.env` and `/opt/caddy/.env`. Run `make bootstrap` again.
