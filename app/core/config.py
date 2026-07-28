"""Application configuration.

All settings come from environment variables (and, when running natively, from
a local ``.env`` file). Nothing here reaches outside the home network, so there
are no auth-related settings — see docs/architecture.md.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LLMProviderName = Literal["none", "ollama", "claude"]
LogFormat = Literal["json", "console"]
AppEnv = Literal["prod", "dev", "test"]

# Repo root when running natively (app/core/config.py -> repo root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ASYNC_DRIVER_PREFIX = "postgresql+asyncpg://"


class Settings(BaseSettings):
    """Typed view over the environment. Instantiate through :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        # .env is shared with Docker Compose and carries compose-only keys
        # (POSTGRES_PASSWORD, WEB_BIND_IP, ...). Without this they would raise.
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_env: AppEnv = "prod"
    app_role: str = "api"  # api | scheduler | script; shows up in pg_stat_activity
    # Wall-clock timezone for cron jobs and for "what day is it" decisions.
    # Containers run in UTC, the family does not: at 03:00 MSK a UTC container
    # thinks it is still yesterday, which would misdate every scraped price.
    app_timezone: str = "Europe/Moscow"

    # --- Database ----------------------------------------------------------
    database_url: str = f"{_ASYNC_DRIVER_PREFIX}ration:ration@db:5432/ration"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle: int = 1800
    db_echo: bool = False

    # --- LLM ---------------------------------------------------------------
    llm_provider: LLMProviderName = "none"
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout_s: float = 120.0
    anthropic_api_key: str = ""
    # Health probes must never hang a request; plan generation gets its own,
    # much larger budget in M5.
    ollama_probe_timeout_s: float = 2.0

    # --- Embeddings (M2) ---------------------------------------------------
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384

    # --- Stores (M3) -------------------------------------------------------
    # NoDecode is required, not decorative: pydantic-settings JSON-decodes
    # complex types inside the settings source, before any field_validator can
    # run, so `STORES=5ka,lenta` raises there. NoDecode hands the raw string to
    # the validator below instead.
    stores: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["5ka", "lenta"])
    fiveka_store_id: str = ""
    lenta_store_id: str = ""
    store_request_delay_s: float = 1.0
    store_timeout_s: float = 30.0
    store_run_timeout_s: int = 3600

    # --- Scheduler ---------------------------------------------------------
    parse_cron_hour: int = 3
    parse_cron_minute: int = 0
    backup_cron_hour: int = 4
    backup_cron_minute: int = 30

    # --- Backups -----------------------------------------------------------
    # Path INSIDE the scheduler container of an optional secondary destination
    # (a NAS share mounted by docker-compose.nas.yml). Empty disables the copy.
    backup_dir: str = ""
    backup_local_dir: Path = Path("/backups")
    backup_keep: int = 14
    backup_timeout_s: int = 1800

    # --- Logging -----------------------------------------------------------
    log_format: LogFormat = "json"
    log_level: str = "INFO"

    @field_validator("stores", mode="before")
    @classmethod
    def _split_stores(cls, value: object) -> object:
        """Accept ``STORES=5ka,lenta``.

        pydantic-settings parses complex types from the environment as JSON, so
        the comma-separated form the spec mandates would otherwise raise at
        import time with an error that never mentions commas.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Reject the sync URL form early, with an actionable message.

        ``postgresql://`` works everywhere else and fails deep inside SQLAlchemy
        with "The asyncio extension requires an async driver to be used".
        """
        if not value.startswith(_ASYNC_DRIVER_PREFIX):
            raise ValueError(
                f"DATABASE_URL must start with '{_ASYNC_DRIVER_PREFIX}' "
                f"(the app is fully async). Got: {value.split('://')[0]}://..."
            )
        return value

    @model_validator(mode="after")
    def _require_claude_key(self) -> Settings:
        """Fail at startup, not at 22:00 during the first plan generation."""
        if self.llm_provider == "claude" and not self.anthropic_api_key.strip():
            raise ValueError("LLM_PROVIDER=claude requires ANTHROPIC_API_KEY to be set")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def backup_to_secondary(self) -> bool:
        """True when the user configured an external backup destination."""
        return bool(self.backup_dir.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none"

    @property
    def sync_database_url(self) -> str:
        """Same database, sync driver — for tools that cannot speak asyncpg."""
        return self.database_url.replace("+asyncpg", "", 1)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached so .env is read once)."""
    return Settings()
