"""Tests for production Redis rate limiting enforcement."""

from __future__ import annotations

import os
import unittest
import unittest.mock

import pytest
from pydantic import ValidationError

from app.config.deployment import DeploymentConfig
from app.config.settings import Settings, clear_config_cache

_MINIMAL_ENV = {
    "API_ID": "12345",
    "API_HASH": "abc123",
    "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # 30-char secret
    "ALLOWED_USER_IDS": "999",
    "ALLOWED_CLIENT_IDS": "test-client",
    "OPENROUTER_API_KEY": "sk-test",
    "FIRECRAWL_API_KEY": "",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "GITHUB_TOKEN_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}


class TestDeploymentConfig(unittest.TestCase):
    def test_default_is_development(self):
        cfg = DeploymentConfig()
        assert cfg.env == "development"
        assert not cfg.api_public_exposure
        assert not cfg.rate_limit_redis_override
        assert not cfg.is_production_mode

    def test_production_env(self):
        cfg = DeploymentConfig.model_validate({"APP_ENV": "production"})
        assert cfg.env == "production"
        assert cfg.is_production_mode

    def test_staging_env(self):
        cfg = DeploymentConfig.model_validate({"APP_ENV": "staging"})
        assert cfg.env == "staging"
        assert not cfg.is_production_mode

    def test_api_public_exposure_triggers_production_mode(self):
        cfg = DeploymentConfig.model_validate({"API_PUBLIC_EXPOSURE": True})
        assert cfg.env == "development"
        assert cfg.is_production_mode

    def test_invalid_env_raises(self):
        with pytest.raises(ValidationError):
            DeploymentConfig.model_validate({"APP_ENV": "invalid"})

    def test_empty_env_defaults_to_development(self):
        cfg = DeploymentConfig.model_validate({"APP_ENV": ""})
        assert cfg.env == "development"


class TestProductionRedisValidation(unittest.TestCase):
    """Settings validator blocks production startup without Redis."""

    def setUp(self):
        clear_config_cache()

    def tearDown(self):
        clear_config_cache()

    def test_dev_without_redis_ok(self):
        """Development mode without Redis should not raise."""
        with unittest.mock.patch.dict(
            os.environ,
            {**_MINIMAL_ENV, "APP_ENV": "development", "REDIS_ENABLED": "false"},
            clear=True,
        ):
            clear_config_cache()
            settings = Settings(allow_stub_telegram=True)
            assert settings.deployment.env == "development"
            assert not settings.redis.enabled

    def test_production_without_redis_enabled_raises(self):
        """Production + REDIS_ENABLED=false must raise RuntimeError at config load."""
        with unittest.mock.patch.dict(
            os.environ,
            {**_MINIMAL_ENV, "APP_ENV": "production", "REDIS_ENABLED": "false"},
            clear=True,
        ):
            clear_config_cache()
            with pytest.raises(RuntimeError, match="REDIS_ENABLED=true"):
                Settings(allow_stub_telegram=True)

    def test_production_without_redis_required_raises(self):
        """Production + REDIS_REQUIRED=false must raise RuntimeError."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                **_MINIMAL_ENV,
                "APP_ENV": "production",
                "REDIS_ENABLED": "true",
                "REDIS_REQUIRED": "false",
            },
            clear=True,
        ):
            clear_config_cache()
            with pytest.raises(RuntimeError, match="REDIS_REQUIRED=true"):
                Settings(allow_stub_telegram=True)

    def test_production_with_redis_required_ok(self):
        """Production + REDIS_REQUIRED=true must not raise."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                **_MINIMAL_ENV,
                "APP_ENV": "production",
                "REDIS_ENABLED": "true",
                "REDIS_REQUIRED": "true",
            },
            clear=True,
        ):
            clear_config_cache()
            settings = Settings(allow_stub_telegram=True)
            assert settings.deployment.is_production_mode
            assert settings.redis.required

    def test_production_with_override_ok(self):
        """RATE_LIMIT_REDIS_OVERRIDE=true bypasses the production Redis requirement."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                **_MINIMAL_ENV,
                "APP_ENV": "production",
                "REDIS_ENABLED": "false",
                "RATE_LIMIT_REDIS_OVERRIDE": "true",
            },
            clear=True,
        ):
            clear_config_cache()
            settings = Settings(allow_stub_telegram=True)
            assert settings.deployment.is_production_mode
            assert settings.deployment.rate_limit_redis_override
            assert not settings.redis.enabled

    def test_api_public_exposure_without_redis_required_raises(self):
        """API_PUBLIC_EXPOSURE=true is treated as production for Redis requirement."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                **_MINIMAL_ENV,
                "APP_ENV": "development",
                "API_PUBLIC_EXPOSURE": "true",
                "REDIS_ENABLED": "true",
                "REDIS_REQUIRED": "false",
            },
            clear=True,
        ):
            clear_config_cache()
            with pytest.raises(RuntimeError, match="REDIS_REQUIRED=true"):
                Settings(allow_stub_telegram=True)

    def test_api_public_exposure_with_redis_required_ok(self):
        """API_PUBLIC_EXPOSURE=true + REDIS_REQUIRED=true must pass."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                **_MINIMAL_ENV,
                "APP_ENV": "development",
                "API_PUBLIC_EXPOSURE": "true",
                "REDIS_ENABLED": "true",
                "REDIS_REQUIRED": "true",
            },
            clear=True,
        ):
            clear_config_cache()
            settings = Settings(allow_stub_telegram=True)
            assert settings.deployment.is_production_mode
            assert settings.redis.required

    def test_staging_without_redis_required_ok(self):
        """Staging environment does not enforce Redis requirement."""
        with unittest.mock.patch.dict(
            os.environ,
            {
                **_MINIMAL_ENV,
                "APP_ENV": "staging",
                "REDIS_ENABLED": "false",
            },
            clear=True,
        ):
            clear_config_cache()
            settings = Settings(allow_stub_telegram=True)
            assert settings.deployment.env == "staging"
            assert not settings.redis.enabled
