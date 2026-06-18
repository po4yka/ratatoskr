"""Tests for GitHubConfig."""

from __future__ import annotations

import os
import unittest.mock
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app.config.github import GitHubConfig
from tests._config_env import MODEL_SELECTION_ENV


def _settings_from_env(**overrides: Any) -> Any:
    from app.config import settings

    return settings.Settings(**overrides)


def test_defaults_load_when_no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GITHUB_REQUEST_TIMEOUT_SEC",
        "GITHUB_README_MAX_BYTES",
        "GITHUB_SYNC_ENABLED",
        "GITHUB_SYNC_CRON",
        "GITHUB_SYNC_LLM_CONCURRENCY",
        "GITHUB_SYNC_LLM_DAILY_BUDGET",
        "GITHUB_OAUTH_APP_CLIENT_ID",
        "GITHUB_OAUTH_APP_CLIENT_SECRET",
        "GITHUB_TOKEN_ENCRYPTION_KEY",
        "GITHUB_CONCURRENCY_PER_USER",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = GitHubConfig()
    assert cfg.request_timeout_sec == 30.0
    assert cfg.readme_max_bytes == 51200
    assert cfg.sync_enabled is True
    assert cfg.sync_cron == "0 2 * * *"
    assert cfg.llm_concurrency == 2
    assert cfg.llm_daily_budget == 100
    assert cfg.oauth_app_client_id is None


def test_env_vars_override_defaults() -> None:
    # GitHubConfig is a BaseModel (not BaseSettings); env vars are wired via
    # Settings._build_nested_from_env using validation_alias. Test the alias
    # round-trip by constructing with the alias keys directly.
    cfg = GitHubConfig.model_validate(
        {
            "GITHUB_SYNC_ENABLED": False,
            "GITHUB_SYNC_LLM_DAILY_BUDGET": 50,
            "GITHUB_OAUTH_APP_CLIENT_ID": "iv1.abc",
        }
    )
    assert cfg.sync_enabled is False
    assert cfg.llm_daily_budget == 50
    assert cfg.oauth_app_client_id == "iv1.abc"


def test_appconfig_includes_github_subconfig() -> None:
    from app.config.settings import AppConfig

    assert "github" in AppConfig.__dataclass_fields__


def test_production_requires_github_token_encryption_key() -> None:
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            # Model selection is required (no code default) for Settings to build.
            **MODEL_SELECTION_ENV,
            "API_ID": "12345",
            "API_HASH": "abc123",
            "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "ALLOWED_USER_IDS": "999",
            "ALLOWED_CLIENT_IDS": "mobile-v1",
            "FIRECRAWL_API_KEY": "",
            "OPENROUTER_API_KEY": "sk-test",
            "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
            "APP_ENV": "production",
            "REDIS_ENABLED": "true",
            "REDIS_REQUIRED": "true",
            # config/ratatoskr.yaml pins redis.required=false; non-secret YAML
            # wins over env per Settings._build_nested_from_env, so the rate-
            # limit validator fires before the GitHub validator unless we opt
            # out explicitly. The override is the documented escape hatch and
            # keeps this test focused on what it actually asserts.
            "RATE_LIMIT_REDIS_OVERRIDE": "true",
            "GITHUB_SYNC_ENABLED": "true",
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with pytest.raises(RuntimeError, match="GITHUB_TOKEN_ENCRYPTION_KEY"):
            _settings_from_env(allow_stub_telegram=True)


def test_production_accepts_github_token_encryption_key() -> None:
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            # Model selection is required (no code default) for Settings to build.
            **MODEL_SELECTION_ENV,
            "API_ID": "12345",
            "API_HASH": "abc123",
            "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "ALLOWED_USER_IDS": "999",
            "ALLOWED_CLIENT_IDS": "mobile-v1",
            "FIRECRAWL_API_KEY": "",
            "OPENROUTER_API_KEY": "sk-test",
            "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
            "APP_ENV": "production",
            "REDIS_ENABLED": "true",
            "REDIS_REQUIRED": "true",
            # See sister test above: ratatoskr.yaml pins redis.required=false,
            # YAML beats env, so we opt out of the rate-limit validator.
            "RATE_LIMIT_REDIS_OVERRIDE": "true",
            "GITHUB_SYNC_ENABLED": "true",
            "GITHUB_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        },
        clear=True,
    ):
        settings.clear_config_cache()
        cfg = _settings_from_env(allow_stub_telegram=True)

    assert cfg.deployment.is_production_mode is True
    assert cfg.github.token_encryption_key is not None
