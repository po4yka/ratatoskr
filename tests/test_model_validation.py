import os
import unittest
from unittest.mock import patch

import pytest

from tests._config_env import MODEL_SELECTION_ENV

# DatabaseConfig now requires a postgresql+asyncpg DSN; provide a syntactically
# valid stub for tests that exercise Settings/load_config without a live DB.
_DATABASE_URL_STUB = "postgresql+asyncpg://test:test@localhost:5432/test"


class TestModelValidation(unittest.TestCase):
    def test_validate_model_name_allows_openrouter_ids(self) -> None:
        from app.config import validate_model_name

        valid_models = [
            "deepseek/deepseek-v4-flash",
            "qwen/qwen3-max",
            "moonshotai/kimi-k2.5",
        ]

        for model in valid_models:
            assert validate_model_name(model) == model

    def test_validate_model_name_rejects_invalid(self) -> None:
        from app.config import validate_model_name

        invalid_models = [
            "evil..model",
            "name<",
            "name>",
            "bad\\name",
            "white space",
            "semi;colon",
        ]

        for model in invalid_models:
            with pytest.raises(ValueError):
                validate_model_name(model)

    def test_load_config_with_openrouter_model_and_fallbacks(self) -> None:
        from app.config import Settings

        # Use Settings directly with _env_file=None to prevent .env file loading
        test_env = {
            # Model selection is required (no code default); supply the baseline,
            # then override the two fields this test asserts on.
            **MODEL_SELECTION_ENV,
            "API_ID": "123456",
            "API_HASH": "a" * 32,
            "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "b" * 20,
            "OPENROUTER_API_KEY": "or_" + "c" * 20,
            "ALLOWED_USER_IDS": "123456789",
            "OPENROUTER_MODEL": "qwen/qwen3-max",
            # fallback/model is valid (no invalid chars), invalid|name has pipe which is invalid
            "OPENROUTER_FALLBACK_MODELS": "fallback/model,google/gemini-3.1-pro-preview, invalid|name",
            # Neutralise the committed config/ratatoskr.yaml: non-secret YAML now
            # overrides env (see _secret_marker.py), but this test asserts env
            # values, so disable YAML loading by pointing at a non-existent file.
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
        }

        with patch.dict(os.environ, test_env, clear=True):
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            cfg = settings.as_app_config()

            # fallback/model is a valid model name (alphanumeric + slash)
            # invalid|name is filtered out (pipe is not in allowed chars)
            assert cfg.openrouter.fallback_models == (
                "fallback/model",
                "google/gemini-3.1-pro-preview",
            )

    def test_load_config_respects_env_overrides(self) -> None:
        from app.config import Settings

        test_env = {
            **MODEL_SELECTION_ENV,
            "API_ID": "123456",
            "API_HASH": "a" * 32,
            "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "f" * 20,
            "OPENROUTER_API_KEY": "or_" + "g" * 20,
            "ALLOWED_USER_IDS": "1001, 1002",
            "OPENROUTER_MAX_TOKENS": "4096",
            "OPENROUTER_TOP_P": "0.75",
            "LOG_LEVEL": "debug",
            "DEBUG_PAYLOADS": "true",
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
        }

        with patch.dict(os.environ, test_env, clear=True):
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            cfg = settings.as_app_config()

            assert cfg.openrouter.max_tokens == 4096
            self.assertAlmostEqual(cfg.openrouter.top_p or 0, 0.75)
            assert cfg.runtime.log_level == "DEBUG"
            assert cfg.runtime.debug_payloads
            assert cfg.telegram.allowed_user_ids == (1001, 1002)

    def test_optional_defaults_apply_while_model_selection_comes_from_env(self) -> None:
        """Genuinely optional fields keep their code defaults, but model selection
        does not: with no model env vars and no YAML it would hard-fail (see
        test_missing_model_selection_hard_fails). Here we supply the model baseline
        and only the optional knobs (temperature, db_path) are left to default.
        """
        from app.config import Settings

        test_env = {
            **MODEL_SELECTION_ENV,
            "API_ID": "123456",
            "API_HASH": "a" * 32,
            "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "h" * 20,
            "OPENROUTER_API_KEY": "or_" + "i" * 20,
            "ALLOWED_USER_IDS": "77",
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
        }

        with patch.dict(os.environ, test_env, clear=True):
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            cfg = settings.as_app_config()

            # Optional knobs still fall back to their code defaults.
            assert cfg.runtime.db_path == "/data/ratatoskr.db"
            assert cfg.openrouter.temperature == 0.2
            # Model selection is sourced from the env baseline, not a code default.
            assert cfg.openrouter.model == MODEL_SELECTION_ENV["OPENROUTER_MODEL"]
            assert cfg.openrouter.flash_model == MODEL_SELECTION_ENV["OPENROUTER_FLASH_MODEL"]
            assert cfg.attachment.vision_model == MODEL_SELECTION_ENV["ATTACHMENT_VISION_MODEL"]

    def test_missing_model_selection_hard_fails(self) -> None:
        """Removing the code defaults makes model selection mandatory: with no
        model env vars and no YAML, building Settings raises rather than silently
        falling back. This locks ratatoskr.yaml as the source of truth.
        """
        from pydantic import ValidationError

        from app.config import Settings

        test_env = {
            "API_ID": "123456",
            "API_HASH": "a" * 32,
            "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "p" * 20,
            "OPENROUTER_API_KEY": "or_" + "q" * 20,
            "ALLOWED_USER_IDS": "77",
            # Deliberately NO OPENROUTER_MODEL / FALLBACK / FLASH / VISION keys.
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
        }

        with patch.dict(os.environ, test_env, clear=True):
            with pytest.raises(ValidationError):
                Settings(_env_file=None)  # type: ignore[call-arg]

    def test_documented_fallback_models_are_known_structured_capable(self) -> None:
        """Drift guard: every model in the documented OpenRouter chain must be
        listed in ModelCapabilities._known_structured_models. Otherwise the chain
        engine's maybe_skip_unsupported_structured_model will silently drop it when
        response_format=json_schema, leaving a dead last-resort fallback (regression
        of incident 640f444f2bcc, where minimax/minimax-m1 was the config default but
        missing from the whitelist).

        Model selection no longer has a code default, so the canonical chain now
        lives in config/ratatoskr.yaml.example (the documented template). This guard
        validates that file's openrouter chain.
        """
        from pathlib import Path

        import yaml

        from app.adapters.openrouter.model_capabilities import ModelCapabilities

        example_path = Path(__file__).resolve().parent.parent / "config" / "ratatoskr.yaml.example"
        openrouter = yaml.safe_load(example_path.read_text(encoding="utf-8"))["openrouter"]
        chain = [
            openrouter["model"],
            *openrouter["fallback_models"],
            openrouter["flash_model"],
            *openrouter["flash_fallback_models"],
            openrouter["long_context_model"],
        ]
        caps = ModelCapabilities(api_key="or_" + "z" * 20, base_url="https://example")

        missing = [m for m in chain if m not in caps._known_structured_models]
        assert missing == [], (
            f"Documented OpenRouter models missing from "
            f"ModelCapabilities._known_structured_models: {missing}. "
            f"Either add them to _known_structured_models or pick a different "
            f"model in config/ratatoskr.yaml.example."
        )

    def test_load_config_allows_stub_credentials(self) -> None:
        from app.config import Settings

        test_env = {
            **MODEL_SELECTION_ENV,
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "j" * 20,
            "OPENROUTER_API_KEY": "or_" + "k" * 20,
        }

        with patch.dict(os.environ, test_env, clear=True):
            # Provide stub telegram credentials directly
            # _env_file and telegram dict are pydantic-settings internals
            settings = Settings(
                _env_file=None,
                allow_stub_telegram=True,
                telegram={  # type: ignore[arg-type]
                    "api_id": 1,
                    "api_hash": "test_api_hash_placeholder_value___",
                    "bot_token": "1000000000:TESTTOKENPLACEHOLDER1234567890ABC",
                    "allowed_user_ids": (),
                },
            )  # type: ignore[call-arg]
            cfg = settings.as_app_config()

            assert cfg.telegram.api_id == 1
            assert cfg.telegram.api_hash.startswith("test_api_hash_placeholder_value")
            assert cfg.telegram.bot_token.startswith("1000000000:")
            assert cfg.telegram.allowed_user_ids == ()

    def test_load_config_requires_allowed_users_when_not_stub(self) -> None:
        from app.config import Settings

        test_env = {
            **MODEL_SELECTION_ENV,
            "API_ID": "123456",
            "API_HASH": "a" * 32,
            "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "l" * 20,
            "OPENROUTER_API_KEY": "or_" + "m" * 20,
            # No ALLOWED_USER_IDS
        }

        with patch.dict(os.environ, test_env, clear=True):
            with pytest.raises(RuntimeError):
                Settings(_env_file=None)  # type: ignore[call-arg]

    def test_load_config_caches_per_process_until_cleared(self) -> None:
        from app.config import clear_config_cache, load_config

        test_env = {
            **MODEL_SELECTION_ENV,
            "API_ID": "123456",
            "API_HASH": "a" * 32,
            "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
            "DATABASE_URL": _DATABASE_URL_STUB,
            "FIRECRAWL_API_KEY": "fc_" + "n" * 20,
            "OPENROUTER_API_KEY": "or_" + "o" * 20,
            "ALLOWED_USER_IDS": "77",
            "LOG_LEVEL": "INFO",
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
        }

        with patch.dict(os.environ, test_env, clear=True):
            clear_config_cache()
            cfg1 = load_config()

            os.environ["LOG_LEVEL"] = "DEBUG"
            cfg2 = load_config()
            assert cfg1 is cfg2
            assert cfg2.runtime.log_level == "INFO"

            clear_config_cache()
            cfg3 = load_config()
            assert cfg3.runtime.log_level == "DEBUG"


if __name__ == "__main__":
    unittest.main()
