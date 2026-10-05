"""Policy test for compose.yaml: co-tenancy, hardening and sandbox protections (V1-20, §9)."""

import re
from pathlib import Path
from typing import Any

import yaml
from research_engine.config import Settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())
RAW = "\n".join(
    line.split("#", 1)[0] for line in (ROOT / "compose.yaml").read_text().splitlines()
)  # comments stripped
SERVICES: dict[str, Any] = COMPOSE["services"]

# Settings fields whose value compose fixes rather than taking from .env
COMPOSE_FIXED = {"SEARXNG_URL", "CRAWL4AI_URL", "DB_PATH"}
SECRETS = {"API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN"}


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0])
    return keys


def test_project_name() -> None:
    assert COMPOSE["name"] == "research-engine"


def test_core_services_without_profiles() -> None:
    assert {n for n, s in SERVICES.items() if not s.get("profiles")} == {
        "app",
        "searxng",
        "crawl4ai",
    }


def test_forbidden_settings() -> None:
    for name, svc in SERVICES.items():
        for key in ("container_name", "privileged", "pid", "ports"):
            assert key not in svc, f"{name} sets {key}"
        assert svc.get("network_mode") != "host"
        assert not any("docker.sock" in str(v) for v in svc.get("volumes", []))
    assert "--no-sandbox" not in RAW and "SYS_ADMIN" not in RAW and "NET_ADMIN" not in RAW


def test_every_service_is_a_good_cotenant() -> None:
    for name, svc in SERVICES.items():
        assert svc["restart"] == "unless-stopped", name
        assert "healthcheck" in svc, name
        opts = svc["logging"]["options"]
        assert "max-size" in opts and "max-file" in opts, name
        limits = svc["deploy"]["resources"]["limits"]
        assert "memory" in limits and "cpus" in limits, name
        if "image" in svc:
            assert re.search(r":[\w.\-${}:]+$", svc["image"]), name
            assert not svc["image"].endswith(":latest"), name


def test_hardening() -> None:
    for name in ("app", "searxng", "crawl4ai"):
        svc = SERVICES[name]
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cap_drop"] == ["ALL"], name
    app = SERVICES["app"]
    assert app["read_only"] is True and app["user"] == "10001:10001"
    assert SERVICES["searxng"]["user"] == "977:977"  # the image's own searxng user
    assert "cap_add" not in SERVICES["searxng"] and "cap_add" not in app
    assert any(str(t).startswith("/tmp") for t in app["tmpfs"])  # noqa: S108 - a tmpfs mount
    assert "app-data:/data" in app["volumes"]
    assert app["stop_grace_period"] == "30s"
    assert "env_file" not in app
    assert [n for n, s in SERVICES.items() if "shm_size" in s] == ["crawl4ai"]
    assert app["depends_on"]["searxng"]["condition"] == "service_healthy"
    assert app["depends_on"]["crawl4ai"]["condition"] == "service_healthy"


def test_crawl4ai_sandbox_protections_present() -> None:
    """ADR-0022: the sandbox depends on these; a compose rewrite must never drop them."""
    c4 = SERVICES["crawl4ai"]
    assert "seccomp=./deploy/crawl4ai/seccomp-chromium.json" in c4["security_opt"]
    assert "./deploy/crawl4ai/addon:/opt/re-addon:ro" in c4["volumes"]
    assert c4["environment"]["PYTHONPATH"] == "/opt/re-addon"
    assert c4["environment"]["CRAWL4AI_CHROMIUM_SANDBOX"] == "true"
    # SYS_CHROOT: Docker's seccomp profile allows chroot(2) only with it, and Chromium's zygote
    # chroots inside its user namespace (ADR-0022). It is in Docker's default set anyway.
    allowed = {"CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER", "SYS_CHROOT"}
    assert set(c4.get("cap_add", [])) <= allowed, (
        "only documented capabilities may be added back; never SYS_ADMIN"
    )
    assert "SYS_ADMIN" not in c4.get("cap_add", [])


def test_networks() -> None:
    assert COMPOSE["networks"]["proxy"] == {"external": True, "name": "proxy"}
    on_proxy = [n for n, s in SERVICES.items() if "proxy" in (s.get("networks") or {})]
    assert on_proxy == ["app"]


def test_volumes_named_only() -> None:
    for name, svc in SERVICES.items():
        for v in svc.get("volumes", []):
            src = str(v).split(":", 1)[0]
            if src.startswith(("./", "/")):
                assert src.startswith("./deploy/") and str(v).endswith(":ro"), f"{name}: {v}"


def test_app_env_covers_every_setting() -> None:
    """Every Settings field reaches the app via an explicit mapping, so they can't drift."""
    env: dict[str, str] = SERVICES["app"]["environment"]
    fields = {f.upper() for f in Settings.model_fields}
    assert set(env) == fields
    for key in fields - COMPOSE_FIXED:
        value = str(env[key])
        if key in SECRETS:
            assert re.fullmatch(rf"\$\{{{key}:\?[^}}]+\}}", value), f"{key}: {value}"
        else:
            assert re.fullmatch(rf"\$\{{{key}:-[^}}]*\}}", value), f"{key}: {value}"
    assert env["DB_PATH"].startswith("/data/")


def test_app_env_defaults_match_settings() -> None:
    env: dict[str, str] = SERVICES["app"]["environment"]
    for name, field in Settings.model_fields.items():
        key = name.upper()
        if key in COMPOSE_FIXED or key in SECRETS:
            continue
        default = re.fullmatch(rf"\$\{{{key}:-([^}}]*)\}}", str(env[key]))
        assert default is not None, key
        assert str(field.default) == default.group(1) or (
            isinstance(field.default, float) and float(default.group(1)) == field.default
        ), f"{key}: compose {default.group(1)!r} != Settings {field.default!r}"


def test_compose_only_secrets_stay_out_of_app() -> None:
    env = SERVICES["app"]["environment"]
    assert "SEARXNG_SECRET" not in env and "LAB_SUBNET" not in env


def test_env_example_in_sync() -> None:
    keys = _env_example_keys()
    assert {f.upper() for f in Settings.model_fields} <= keys
    compose_vars = set(re.findall(r"\$\{(\w+)[:}]", RAW))
    assert compose_vars <= keys, "every ${VAR} in compose.yaml is documented in .env.example"
