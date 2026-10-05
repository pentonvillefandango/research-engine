import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine_client.models import SearchIntent

from tests.conftest import TEST_API_KEY

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ONLY = {
    "SEARXNG_SECRET",
    "APP_PORT",
    *(f"{svc}_{lim}" for svc in ("APP", "SEARXNG", "CRAWL4AI") for lim in ("MEM_LIMIT", "CPUS")),
}


def test_requires_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_reads_env(settings_env: None) -> None:
    s = Settings()  # type: ignore[call-arg]
    assert s.api_key.get_secret_value() == TEST_API_KEY
    assert s.site_host == "research.localhost"
    assert s.search_min_results == 10 and s.thin_word_threshold == 150


def test_allow_hosts_parsing(settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", " Intranet.Example , docs.local ")
    s = Settings()  # type: ignore[call-arg]
    assert s.ssrf_allow_hosts_set == frozenset({"intranet.example", "docs.local"})


def test_env_example_covers_settings() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    fields = {name.upper() for name in Settings.model_fields}
    assert fields - keys == set(), "add missing settings to .env.example"
    assert keys - fields - COMPOSE_ONLY == set(), "unknown keys in .env.example"


def test_intents_load_all() -> None:
    reg = IntentRegistry.load(ROOT / "config" / "intents.yaml")
    assert set(reg.all()) == set(SearchIntent)
    assert reg.get(SearchIntent.TECHNICAL).engines


def test_intent_engines_exist_in_searxng() -> None:
    enabled = set(
        json.loads((ROOT / "tests/fixtures/searxng/engines_config.json").read_text())["enabled"]
    )
    reg = IntentRegistry.load(ROOT / "config" / "intents.yaml")
    used = {e for p in reg.all().values() for e in p.engines}
    assert used <= enabled, f"engines not enabled in SearXNG: {sorted(used - enabled)}"


def test_intents_reject_unknown(tmp_path: Path) -> None:
    bad = tmp_path / "i.yaml"
    bad.write_text("intents:\n  nonsense: {categories: [general], engines: [], description: x}\n")
    with pytest.raises(ValueError):
        IntentRegistry.load(bad)


def test_intents_reject_missing(tmp_path: Path) -> None:
    bad = tmp_path / "i.yaml"
    bad.write_text("intents:\n  general: {categories: [general], engines: [], description: x}\n")
    with pytest.raises(ValueError, match="missing"):
        IntentRegistry.load(bad)


@pytest.fixture
def ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITE_HOST", "ambient.example")
    monkeypatch.setenv("SEARCH_MIN_RESULTS", "99")
    monkeypatch.setenv("THIN_WORD_THRESHOLD", "1")


def test_settings_env_isolated_from_ambient(ambient_env: None, settings_env: None) -> None:
    s = Settings()  # type: ignore[call-arg]
    assert s.site_host == "research.localhost"
    assert s.search_min_results == 10 and s.thin_word_threshold == 150


def test_settings_ignores_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("API_KEY=x\nSESSION_SECRET=y\nCRAWL4AI_API_TOKEN=z\n")
    monkeypatch.chdir(tmp_path)
    for k in ("API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


SECRET_VARS = ("API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN")


@pytest.mark.parametrize("name", SECRET_VARS)
@pytest.mark.parametrize("bad", ["change-me", "", "s" * 31, "unique-short-value-" + "q" * 5])
def test_refuses_placeholder_or_short_secret(
    settings_env: None, monkeypatch: pytest.MonkeyPatch, name: str, bad: str
) -> None:
    monkeypatch.setenv(name, bad)
    with pytest.raises(ValidationError) as ei:
        Settings()  # type: ignore[call-arg]
    msg = str(ei.value)
    assert name in msg and "openssl rand -hex 32" in msg and "32 characters" in msg
    if bad:
        assert bad not in msg  # the value itself is never echoed


@pytest.mark.parametrize("name", SECRET_VARS)
def test_accepts_32_char_secret(
    settings_env: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    value = "k" * 32  # built at runtime: a fake key, nothing for gitleaks to flag
    monkeypatch.setenv(name, value)
    assert getattr(Settings(), name.lower()).get_secret_value() == value  # type: ignore[call-arg]
