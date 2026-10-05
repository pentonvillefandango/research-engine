"""Policy test for the compose files: co-tenancy, hardening and sandbox protections (V1-20, §9)."""

import posixpath
import re
from pathlib import Path
from typing import Any

import yaml
from research_engine.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text())


def _raw(name: str) -> str:
    """The file with comments stripped."""
    return "\n".join(line.split("#", 1)[0] for line in (ROOT / name).read_text().splitlines())


COMPOSE = _load("compose.yaml")
RAW = _raw("compose.yaml")
SERVICES: dict[str, Any] = COMPOSE["services"]
DEV = _load("compose.dev.yaml")
DEBUG = _load("compose.debug.yaml")

# Settings fields whose value compose fixes rather than taking from .env
COMPOSE_FIXED = {"SEARXNG_URL", "CRAWL4AI_URL", "DB_PATH"}
SECRETS = {"API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN"}
SECCOMP_PROFILE = "./deploy/crawl4ai/seccomp-chromium.json"


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0])
    return keys


def _mounts(svc: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise short- and long-syntax volume entries to {type, source, target, read_only}."""
    out: list[dict[str, Any]] = []
    for v in svc.get("volumes", []):
        if isinstance(v, dict):
            out.append(
                {
                    "type": v.get("type"),
                    "source": str(v.get("source", "")),
                    "target": str(v.get("target", "")),
                    "read_only": v.get("read_only") is True,
                }
            )
            continue
        parts = str(v).split(":")
        src, tgt = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
        mode = parts[2] if len(parts) > 2 else ""
        bind = src.startswith((".", "/", "~", "$"))
        out.append(
            {
                "type": "bind" if bind else ("volume" if src else "anonymous"),
                "source": src,
                "target": tgt,
                "read_only": "ro" in mode.split(","),
            }
        )
    return out


def _security_opts(svc: dict[str, Any]) -> list[tuple[str, str]]:
    """security_opt entries as (key, value); Docker accepts both `key=value` and `key:value`."""
    out: list[tuple[str, str]] = []
    for opt in svc.get("security_opt", []):
        key, _, value = str(opt).strip().replace(":", "=", 1).partition("=")
        out.append((key.strip().lower(), value.strip()))
    return out


def test_project_name() -> None:
    assert COMPOSE["name"] == "research-engine"


def test_core_services_without_profiles() -> None:
    assert {n for n, s in SERVICES.items() if not s.get("profiles")} == {
        "app",
        "searxng",
        "crawl4ai",
    }


def test_forbidden_settings() -> None:
    forbidden = (
        "container_name",
        "privileged",
        "pid",
        "ports",
        "devices",
        "network_mode",  # host, service:..., container:... all bypass the project networks
        "volumes_from",
    )
    for name, svc in SERVICES.items():
        for key in forbidden:
            assert key not in svc, f"{name} sets {key}"
        assert svc.get("ipc") not in ("host",) and not str(svc.get("ipc", "")).startswith(
            ("container:", "service:")
        ), f"{name} shares ipc"
        assert svc.get("userns_mode") != "host", f"{name} sets userns_mode: host"
        assert svc.get("cgroup") != "host", f"{name} sets cgroup: host"
    assert "--no-sandbox" not in RAW and "SYS_ADMIN" not in RAW and "NET_ADMIN" not in RAW


def test_no_docker_socket() -> None:
    for name, svc in SERVICES.items():
        for m in _mounts(svc):
            assert "docker.sock" not in m["source"] + m["target"], f"{name}: {m}"


def test_every_service_is_a_good_cotenant() -> None:
    for name, svc in SERVICES.items():
        assert svc["restart"] == "unless-stopped", name
        assert "healthcheck" in svc, name
        opts = svc["logging"]["options"]
        assert "max-size" in opts and "max-file" in opts, name
        limits = svc["deploy"]["resources"]["limits"]
        assert "memory" in limits and "cpus" in limits, name


def test_images_pinned() -> None:
    for name, svc in SERVICES.items():
        if "image" not in svc:
            continue
        image = str(svc["image"])
        tag = image.rsplit("/", 1)[-1].partition(":")[2]
        assert tag, f"{name}: no tag"
        assert "latest" not in tag.lower(), f"{name}: {image}"
        for default in re.findall(r"\$\{\w+(?::?-([^}]*))?\}", tag):
            assert default.strip(), f"{name}: interpolated tag needs a non-empty default"


def test_hardening() -> None:
    for name in ("app", "searxng", "crawl4ai"):
        svc = SERVICES[name]
        assert ("no-new-privileges", "true") in _security_opts(svc), name
        assert svc["cap_drop"] == ["ALL"], name
    app = SERVICES["app"]
    assert app["read_only"] is True and app["user"] == "10001:10001"
    assert SERVICES["searxng"]["user"] == "977:977"  # the image's own searxng user
    assert "app-data:/data" in app["volumes"]
    assert app["stop_grace_period"] == "30s"
    assert "env_file" not in app
    assert [n for n, s in SERVICES.items() if "shm_size" in s] == ["crawl4ai"]
    assert app["depends_on"]["searxng"]["condition"] == "service_healthy"
    assert app["depends_on"]["crawl4ai"]["condition"] == "service_healthy"


def test_app_tmpfs() -> None:
    tmpfs = SERVICES["app"]["tmpfs"]
    tmpfs = [tmpfs] if isinstance(tmpfs, str) else tmpfs
    assert len(tmpfs) == 1
    target, _, opts = str(tmpfs[0]).partition(":")
    assert target == "/tmp"  # noqa: S108 - a tmpfs mount target
    assert {"noexec", "nosuid", "nodev"} <= set(opts.split(","))


def test_capabilities() -> None:
    # SYS_CHROOT only (ADR-0022): Docker's seccomp profile allows chroot(2) only with it, and
    # Chromium's zygote chroots inside its user namespace. Any addition needs an ADR change.
    assert SERVICES["crawl4ai"]["cap_add"] == ["SYS_CHROOT"]
    assert [n for n, s in SERVICES.items() if "cap_add" in s] == ["crawl4ai"]


def test_security_opt_never_weakened() -> None:
    """Docker applies the last seccomp= entry: an extra `unconfined` silently disables a layer."""
    for name, svc in SERVICES.items():
        for key, value in _security_opts(svc):
            assert "unconfined" not in value.lower(), f"{name}: {key}={value}"
            assert not (key == "label" and value.lower() == "disable"), f"{name}: label disable"
            assert key in ("no-new-privileges", "seccomp", "apparmor", "label"), f"{name}: {key}"
        seccomp = [v for k, v in _security_opts(svc) if k == "seccomp"]
        assert seccomp == ([SECCOMP_PROFILE] if name == "crawl4ai" else []), f"{name}: {seccomp}"


def test_crawl4ai_sandbox_protections_present() -> None:
    """ADR-0022: the sandbox depends on these; a compose rewrite must never drop them."""
    c4 = SERVICES["crawl4ai"]
    assert f"seccomp={SECCOMP_PROFILE}" in c4["security_opt"]
    assert "./deploy/crawl4ai/addon:/opt/re-addon:ro" in c4["volumes"]
    assert c4["environment"]["PYTHONPATH"] == "/opt/re-addon"
    assert c4["environment"]["CRAWL4AI_CHROMIUM_SANDBOX"] == "true"


def test_networks() -> None:
    assert COMPOSE["networks"]["proxy"] == {"external": True, "name": "proxy"}
    on_proxy = [n for n, s in SERVICES.items() if "proxy" in (s.get("networks") or {})]
    assert on_proxy == ["app"]


def test_volumes_named_only() -> None:
    declared = set(COMPOSE.get("volumes") or {})
    for name, svc in SERVICES.items():
        for m in _mounts(svc):
            if m["type"] == "volume":
                assert m["source"] in declared, f"{name}: undeclared volume {m}"
                continue
            assert m["type"] == "bind", f"{name}: {m}"
            src = m["source"]
            assert ".." not in src.split("/"), f"{name}: path escape {src}"
            norm = posixpath.normpath(src)
            assert norm.startswith("deploy/"), f"{name}: bind outside ./deploy: {src}"
            assert src.startswith("./deploy/") and m["read_only"], f"{name}: {m}"


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


def test_dev_override_publishes_loopback_ephemeral_only() -> None:
    assert set(DEV) <= {"services", "networks"}
    for name, svc in DEV["services"].items():
        if name == "app":
            continue  # image only: see test_dev_override_app_image_is_dev_only
        assert set(svc) == {"ports"}, f"dev override may only add ports: {name}"
        for p in svc["ports"]:
            assert isinstance(p, str) and re.fullmatch(r"127\.0\.0\.1::\d+", p), f"{name}: {p}"
    assert set(DEV.get("networks", {})) == {"proxy"}
    proxy = DEV["networks"]["proxy"]
    assert proxy["external"] is False and str(proxy["name"]).startswith("research-engine-")


def test_dev_override_app_image_is_dev_only() -> None:
    """A dev `up --build` must never overwrite a live or rollback `research-engine-app:<sha>`
    tag: the dev override renames the app image, with no variable in it, and changes nothing
    else about the app (no ports)."""
    app = DEV["services"]["app"]
    assert set(app) == {"image"}
    image = app["image"]
    assert "$" not in image and ":" in image
    repo, _, _tag = image.partition(":")
    assert repo != COMPOSE["services"]["app"]["image"].partition(":")[0]
    assert repo.startswith("research-engine-") and repo.endswith("-dev")


def test_debug_override_app_loopback_only() -> None:
    assert set(DEBUG) == {"services"}
    assert set(DEBUG["services"]) == {"app"}
    app = DEBUG["services"]["app"]
    assert set(app) == {"ports"}
    assert len(app["ports"]) == 1
    assert re.fullmatch(r"127\.0\.0\.1:\$\{APP_PORT:\?[^}]+\}:8000", app["ports"][0])
