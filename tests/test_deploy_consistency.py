"""Cross-file deploy consistency checks (V1-20, step-8 review M-5)."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())
SITE = "\n".join(
    line.split("#", 1)[0]
    for line in (ROOT / "deploy" / "caddy" / "sites" / "research-engine.caddy")
    .read_text()
    .splitlines()
)
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_proxy_alias_matches_caddy_upstream() -> None:
    aliases = COMPOSE["services"]["app"]["networks"]["proxy"]["aliases"]
    upstream = re.findall(r"reverse_proxy\s+([\w.-]+):\d+", SITE)
    assert len(upstream) == 1, "exactly one reverse_proxy upstream in the site file"
    assert upstream[0] in aliases, "Caddy upstream must be a proxy alias of the app service"
    assert upstream[0].startswith("research-engine-"), "tool-prefixed alias, never a bare name"


def test_dockerfile_and_compose_healthchecks_agree() -> None:
    match = re.search(r"^HEALTHCHECK\b.*?\\\n\s*CMD (\[.*\])\s*$", DOCKERFILE, re.M)
    assert match, "Dockerfile HEALTHCHECK with exec-form CMD"
    import json

    docker_cmd: list[str] = json.loads(match.group(1))
    compose_test: list[str] = COMPOSE["services"]["app"]["healthcheck"]["test"]
    assert compose_test == ["CMD", *docker_cmd]
    url = re.search(r"urlopen\('([^']+)'", docker_cmd[-1])
    assert url and url.group(1) == "http://127.0.0.1:8000/health"


def test_lab_subnet_not_in_app_env_example() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    assert "LAB_SUBNET" not in keys, "LAB_SUBNET lives only in deploy/caddy/.env.example"
