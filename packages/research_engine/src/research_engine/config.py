"""Service configuration from environment variables (V1-18). Compose injects .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "change-me"  # noqa: S105  # the .env.example placeholder, refused below
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a secret's value.
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    # core / identity
    api_key: SecretStr
    session_secret: SecretStr
    site_host: str = "research.localhost"
    git_sha: str = "unknown"
    log_level: str = "INFO"
    user_agent: str = "ResearchEngine/1.0 (+https://github.com/pentonvillefandango/research-engine)"

    # upstreams
    searxng_url: str = "http://searxng:8080"
    crawl4ai_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: SecretStr

    # config files
    intents_file: Path = Path("config/intents.yaml")
    demos_file: Path = Path("config/demos.yaml")

    # search
    search_min_results: int = Field(default=10, ge=1)
    search_timeout_s: float = Field(default=30.0, gt=0)

    # fetch
    page_timeout_s: int = Field(default=60, ge=1)
    thin_word_threshold: int = Field(default=150, ge=0)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    allowed_content_types: str = "text/html,application/xhtml+xml,application/pdf,text/plain"
    ssrf_allow_hosts: str = ""
    domain_concurrency: int = Field(default=2, ge=1)
    domain_delay_s: float = Field(default=1.0, ge=0)

    # cache
    cache_ttl_search_s: int = Field(default=3600, ge=0)
    cache_ttl_page_s: int = Field(default=86400, ge=0)

    # store / jobs / events
    db_path: str = "data/research-engine.sqlite"
    job_workers: int = Field(default=2, ge=1)
    job_fetch_concurrency: int = Field(default=5, ge=1)
    job_timeout_s: int = Field(default=900, ge=1)
    event_retention_days: int = Field(default=30, ge=1)

    @field_validator("api_key", "session_secret", "crawl4ai_api_token")
    @classmethod
    def _real_secret(cls, v: SecretStr, info: ValidationInfo) -> SecretStr:
        """Refuse the published placeholder and short secrets (names the variable, never the
        value): with SESSION_SECRET=change-me anyone could mint a GUI session cookie."""
        name = (info.field_name or "secret").upper()
        value = v.get_secret_value()
        if value == PLACEHOLDER_SECRET or len(value) < MIN_SECRET_LENGTH:
            what = (
                "is the .env.example placeholder"
                if value == PLACEHOLDER_SECRET
                else f"is shorter than {MIN_SECRET_LENGTH} characters"
            )
            raise ValueError(
                f"{name} {what}; it must be a random secret of at least {MIN_SECRET_LENGTH} "
                "characters. Generate one with: openssl rand -hex 32"
            )
        return v

    @property
    def ssrf_allow_hosts_set(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.ssrf_allow_hosts.split(",") if h.strip())

    @property
    def allowed_content_types_set(self) -> frozenset[str]:
        return frozenset(
            t.strip().lower() for t in self.allowed_content_types.split(",") if t.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
