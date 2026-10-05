"""Policy test for the shared Caddy project in deploy/caddy/ (V1-20, §9, D15, D17)."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CADDY = ROOT / "deploy" / "caddy"
COMPOSE: dict[str, Any] = yaml.safe_load((CADDY / "compose.yaml").read_text())
SVC: dict[str, Any] = COMPOSE["services"]["caddy"]


def _strip(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


CADDYFILE = _strip((CADDY / "Caddyfile").read_text())
SITE = _strip((CADDY / "sites" / "research-engine.caddy").read_text())
COMPOSE_RAW = _strip((CADDY / "compose.yaml").read_text())


def _files() -> list[Path]:
    return [p for p in CADDY.rglob("*") if p.is_file() and p.name != ".env"]


def test_project_and_image() -> None:
    assert COMPOSE["name"] == "caddy"
    assert set(COMPOSE["services"]) == {"caddy"}
    assert SVC["image"] == "caddy:2.11.6"
    assert "latest" not in COMPOSE_RAW.lower()


def test_ports_and_network() -> None:
    # The one place fixed host ports are allowed: the shared front door (§9).
    assert SVC["ports"] == ["80:80", "443:443", "443:443/udp"]
    assert COMPOSE["networks"]["proxy"] == {"name": "proxy"}  # created here, not external
    assert SVC["networks"] == ["proxy"]


def test_volumes() -> None:
    assert set(COMPOSE["volumes"]) == {"caddy-data", "caddy-config"}
    assert sorted(SVC["volumes"]) == sorted(
        [
            "caddy-data:/data",
            "caddy-config:/config",
            "./Caddyfile:/etc/caddy/Caddyfile:ro",
            "./sites:/etc/caddy/sites:ro",
            "./index:/srv/index:ro",
        ]
    )
    assert "docker.sock" not in COMPOSE_RAW


def test_good_cotenant() -> None:
    for key in ("container_name", "privileged", "pid", "devices", "network_mode", "volumes_from"):
        assert key not in SVC, key
    assert SVC["restart"] == "unless-stopped"
    assert SVC["env_file"] in (".env", [".env"])
    opts = SVC["logging"]["options"]
    assert "max-size" in opts and "max-file" in opts
    limits = SVC["deploy"]["resources"]["limits"]
    assert limits == {"memory": "256m", "cpus": "0.5"}
    assert "http://127.0.0.1:2019/config/" in " ".join(SVC["healthcheck"]["test"])


def test_hardening() -> None:
    assert "no-new-privileges:true" in SVC["security_opt"]
    assert SVC["cap_drop"] == ["ALL"]
    assert SVC["cap_add"] == ["NET_BIND_SERVICE"]


def test_caddyfile() -> None:
    assert "admin 127.0.0.1:2019" in CADDYFILE
    assert "import sites/*.caddy" in CADDYFILE
    assert re.search(r"\{\$TOOLBOX_HOST:toolbox\.home\.arpa\}\s*\{", CADDYFILE)
    assert "tls internal" in CADDYFILE and "root * /srv/index" in CADDYFILE
    assert "file_server" in CADDYFILE


def test_site_file() -> None:
    assert "{$SITE_HOST}" in SITE and "tls internal" in SITE
    assert "@lab remote_ip {$LAB_SUBNET}" in SITE
    assert "handle @lab" in SITE
    assert "reverse_proxy research-engine-app:8000" in SITE
    assert "flush_interval -1" in SITE
    assert "respond 403" in SITE or "respond * 403" in SITE


def test_no_trusted_proxies() -> None:
    """The app's Origin check needs Caddy to set X-Forwarded-* from the real client."""
    for text in (CADDYFILE, SITE):
        assert "trusted_proxies" not in text
    for p in _files():
        if p.suffix != ".md":  # the README explains why it is absent
            assert "trusted_proxies" not in _strip(p.read_text()), p


def test_no_hardcoded_ips_or_hosts() -> None:
    ipv4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
    docs_ranges = ("192.0.2.", "198.51.100.", "203.0.113.")
    for p in _files():
        for line in p.read_text().splitlines():
            code, _, comment = line.partition("#")
            for m in ipv4.finditer(code):
                assert m.group(0) == "127.0.0.1", f"{p}: {line}"
            for m in ipv4.finditer(comment):
                ip = m.group(0)
                assert ip == "127.0.0.1" or ip.startswith(docs_ranges), f"{p}: {line}"
    # real-looking hostnames only as the documented examples
    allowed = {"toolbox.home.arpa", "research.toolbox.home.arpa"}
    for p in _files():
        for host in re.findall(r"[\w-]+(?:\.[\w-]+)*\.home\.arpa", p.read_text()):
            assert host in allowed, f"{p}: {host}"
    assert "research.toolbox.home.arpa" not in CADDYFILE + SITE + COMPOSE_RAW


def test_env_example_and_index() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in (CADDY / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert keys == {"SITE_HOST", "LAB_SUBNET", "TOOLBOX_HOST"}
    html = (CADDY / "index" / "index.html").read_text()
    assert "Tools on this host" in html and "<!--" in html


def test_readme_covers_required_topics() -> None:
    readme = (CADDY / "README.md").read_text()
    for needle in (
        "/opt/caddy",
        "docker compose up -d",
        "caddy reload --config /etc/caddy/Caddyfile",
        "http://{$SITE_HOST}",
        "security add-trusted-cert",
        "X-Forwarded-Proto",
        "ADR-0026",
    ):
        assert needle in readme, needle
