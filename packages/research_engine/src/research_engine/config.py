"""Service configuration from environment variables (V1-18). Compose injects .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # core / identity
    api_key: SecretStr
    session_secret: SecretStr
    site_host: str = "research.localhost"
    git_sha: str = "unknown"
    log_level: str = "INFO"
    user_agent: str = "ResearchEngine/0.1 (+https://github.com/pentonvillefandango/research-engine)"

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
