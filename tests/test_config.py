"""Settings parsing — the two validators that would otherwise fail at startup."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    # _env_file=None isolates the test from the developer's real .env.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_stores_accepts_the_comma_separated_form_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec writes STORES=5ka,lenta; pydantic-settings would JSON-decode it."""
    monkeypatch.setenv("STORES", "5ka, lenta ,magnit")
    assert _settings().stores == ["5ka", "lenta", "magnit"]


def test_stores_ignores_blank_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORES", "5ka,,")
    assert _settings().stores == ["5ka"]


def test_sync_database_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg://"):
        _settings(database_url="postgresql://ration:ration@db:5432/ration")


def test_claude_provider_requires_a_key() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        _settings(llm_provider="claude", anthropic_api_key="")


def test_claude_provider_accepts_a_key() -> None:
    settings = _settings(llm_provider="claude", anthropic_api_key="sk-test")
    assert settings.llm_enabled is True


def test_backup_secondary_is_off_by_default() -> None:
    assert _settings().backup_to_secondary is False
    assert _settings(backup_dir="/mnt/nas-backup").backup_to_secondary is True


def test_log_level_is_normalised() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"
