"""Precedence matrix for the env/YAML split introduced by `_secret_marker`.

The post-refactor convention is:

* secret env  >  defaults                            (YAML secret keys ignored)
* non-secret YAML  >  os.environ  >  defaults        (YAML wins for non-secrets)

These tests pin the matrix end-to-end via the real Settings constructor so
the wiring in ``app/config/settings.py:_build_nested_from_env`` cannot
regress silently.
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.uses_real_yaml
from pathlib import Path
from textwrap import dedent

import pytest

from app.config import Settings
from app.config._secret_marker import (
    SECRET_MARKER,
    collect_secret_env_names,
    filter_yaml_to_non_secrets,
    is_secret_field,
)

# Minimal env the Settings model demands to instantiate at all (TelegramConfig
# and OpenRouterConfig have required fields). Tests overlay their own values
# on top of this baseline via monkeypatch.
_BASELINE_ENV: dict[str, str] = {
    "API_ID": "123456",
    "API_HASH": "a" * 32,
    "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
    "ALLOWED_USER_IDS": "123",
    "OPENROUTER_API_KEY": "or_" + "b" * 20,
    "FIRECRAWL_API_KEY": "fc-" + "f" * 40,
    "DATABASE_URL": "postgresql+asyncpg://test:test@postgres:5432/test",
}


def _apply_baseline(mp: pytest.MonkeyPatch) -> None:
    """Drop the host's ambient env, then set just enough for Settings."""
    import os

    for key in tuple(os.environ):
        mp.delenv(key, raising=False)
    for key, value in _BASELINE_ENV.items():
        mp.setenv(key, value)


def _write_yaml(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "ratatoskr.yaml"
    cfg.write_text(dedent(body), encoding="utf-8")
    return cfg


class TestSecretMarkerHelpers:
    """Unit tests for the `_secret_marker` module's primitives."""

    def test_is_secret_field_recognises_marker(self) -> None:
        from pydantic import BaseModel, Field

        class _Model(BaseModel):
            sensitive: str = Field(default="", json_schema_extra=SECRET_MARKER)
            public: str = Field(default="")

        assert is_secret_field(_Model.model_fields["sensitive"]) is True
        assert is_secret_field(_Model.model_fields["public"]) is False

    def test_is_secret_field_with_non_dict_extra(self) -> None:
        from pydantic import BaseModel, Field

        # json_schema_extra accepts callables; the helper must not crash and
        # must NOT treat such a field as a secret (callable carries no
        # discoverable marker).
        class _Model(BaseModel):
            quirky: str = Field(default="", json_schema_extra=lambda schema: None)

        assert is_secret_field(_Model.model_fields["quirky"]) is False

    def test_collect_secret_env_names_walks_nested_models(self) -> None:
        names = collect_secret_env_names(Settings)
        # Spot-check: the canonical secrets must be present.
        for required in (
            "OPENROUTER_API_KEY",
            "API_HASH",
            "BOT_TOKEN",
            "DATABASE_URL",
            "JWT_SECRET_KEY",
            "FIRECRAWL_API_KEY",
        ):
            assert required in names, f"{required} should be marked secret"
        # AliasChoices entries are surfaced too.
        assert "JWT_SECRET" in names
        assert "TELEGRAM_API_HASH" in names

    def test_collect_secret_env_names_excludes_non_secrets(self) -> None:
        names = collect_secret_env_names(Settings)
        for non_secret in (
            "OPENROUTER_MODEL",
            "LOG_LEVEL",
            "ARTICLE_VISION_MIN_IMAGES",
            "SUMMARIZATION_MAX_RETRIES",
        ):
            assert non_secret not in names, f"{non_secret} must NOT be classified as a secret"

    def test_filter_yaml_to_non_secrets_splits_correctly(self) -> None:
        yaml = {
            "OPENROUTER_API_KEY": "leaked-key",
            "OPENROUTER_MODEL": "qwen/qwen3-max",
            "LOG_LEVEL": "DEBUG",
        }
        non_secret, secret = filter_yaml_to_non_secrets(yaml, {"OPENROUTER_API_KEY"})
        assert non_secret == {
            "OPENROUTER_MODEL": "qwen/qwen3-max",
            "LOG_LEVEL": "DEBUG",
        }
        assert secret == {"OPENROUTER_API_KEY": "leaked-key"}


class TestPrecedenceMatrix:
    """End-to-end Settings load with mixed env/YAML inputs."""

    def test_non_secret_yaml_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_yaml(
            tmp_path,
            """\
            runtime:
              log_level: DEBUG
            openrouter:
              model: qwen/qwen3-max
            """,
        )
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            mp.setenv("LOG_LEVEL", "WARNING")  # env: WARNING
            mp.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")  # env value
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        assert cfg_obj.runtime.log_level == "DEBUG"  # YAML wins
        assert cfg_obj.openrouter.model == "qwen/qwen3-max"  # YAML wins

    def test_env_wins_when_yaml_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Back-compat: if YAML doesn't mention a key, env still works.
        cfg = _write_yaml(
            tmp_path,
            """\
            runtime:
              chunk_max_chars: 999999
            """,
        )
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            mp.setenv("LOG_LEVEL", "ERROR")
            mp.setenv("OPENROUTER_MODEL", "x-ai/grok-4")
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        # YAML didn't override these; env wins.
        assert cfg_obj.runtime.log_level == "ERROR"
        assert cfg_obj.openrouter.model == "x-ai/grok-4"
        # YAML's runtime.chunk_max_chars DID apply.
        assert cfg_obj.runtime.chunk_max_chars == 999_999

    def test_secret_in_env_wins_over_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even if a credential ends up in YAML (a security misconfiguration),
        # the env value must win and the YAML one is dropped.
        cfg = _write_yaml(
            tmp_path,
            """\
            openrouter:
              api_key: or_yaml_leaked_key
            """,
        )
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            mp.setenv("OPENROUTER_API_KEY", "or_" + "z" * 30)
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        assert cfg_obj.openrouter.api_key == "or_" + "z" * 30

    def test_secret_in_yaml_is_logged_and_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The Settings loader logs a single warning naming every YAML key
        # that was marked secret and dropped.
        cfg = _write_yaml(
            tmp_path,
            """\
            firecrawl:
              api_key: fc-yaml-leaked-key
            """,
        )
        env_api_key = "fc-" + "a" * 40
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            mp.setenv("FIRECRAWL_API_KEY", env_api_key)
            with caplog.at_level(logging.WARNING):
                cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        assert cfg_obj.firecrawl.api_key == env_api_key
        warnings = [r for r in caplog.records if "yaml_secret_keys_ignored" in r.getMessage()]
        assert warnings, (
            "Expected a yaml_secret_keys_ignored warning when a credential leaks into YAML"
        )
        # The warning record names the offending key in its extra payload.
        assert "FIRECRAWL_API_KEY" in " ".join(str(r.__dict__) for r in warnings)

    def test_default_used_when_neither_env_nor_yaml_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            # Point RATATOSKR_CONFIG at a path that doesn't exist so the loader
            # cannot fall back to the repo-local config/ratatoskr.yaml.
            mp.setenv("RATATOSKR_CONFIG", "/nonexistent/ratatoskr.yaml")
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        # ATTACHMENT_MAX_DOCUMENT_CHARS default is 45000. The repo's
        # ratatoskr.yaml does not pin this field (it only sets
        # article_vision_min_images and vision_routing_role_filter_enabled
        # under `attachment:`) so the default must reach the AppConfig
        # unchanged. Pick a YAML-absent field deliberately — verifying the
        # default-wins-without-override precedence requires that no overlay
        # touch it.
        assert cfg_obj.attachment.max_document_chars == 45000


class TestYamlDictRoundTrip:
    """Dict-typed YAML fields must round-trip without the env-string workaround.

    Regression guard for the ``_serialize_value`` fix: a YAML dict must reach
    the field validator as a native ``dict``, not as a JSON string (which would
    silently drop every entry because the validator's string path expects
    ``"model=seconds"`` pairs, not JSON).
    """

    def test_llm_per_model_timeout_overrides_dict_form(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Natural dict YAML form is accepted and produces correct float values."""
        cfg = _write_yaml(
            tmp_path,
            """\
            runtime:
              llm_per_model_timeout_overrides:
                deepseek/deepseek-v3.2: 180
                qwen/qwen3-max: 120
            """,
        )
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        overrides = cfg_obj.runtime.llm_per_model_timeout_overrides
        assert overrides == {
            "deepseek/deepseek-v3.2": 180.0,
            "qwen/qwen3-max": 120.0,
        }

    def test_per_model_max_tokens_overrides_dict_form(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """openrouter.per_model_max_tokens_overrides accepts the dict YAML form."""
        cfg = _write_yaml(
            tmp_path,
            """\
            openrouter:
              per_model_max_tokens_overrides:
                qwen/qwen3-vl-32b-instruct: 3072
                deepseek/deepseek-v4-pro: 8192
            """,
        )
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        overrides = cfg_obj.openrouter.per_model_max_tokens_overrides
        assert overrides == {
            "qwen/qwen3-vl-32b-instruct": 3072,
            "deepseek/deepseek-v4-pro": 8192,
        }

    def test_env_string_form_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy env-var string form ``model=N,model=N`` still parses correctly.

        Uses an empty tmp-YAML to neutralise the committed ``config/ratatoskr.yaml``
        which otherwise wins over env for this key under the new precedence.
        """
        cfg = _write_yaml(tmp_path, "")
        with pytest.MonkeyPatch.context() as mp:
            _apply_baseline(mp)
            mp.setenv("RATATOSKR_CONFIG", str(cfg))
            mp.setenv(
                "LLM_PER_MODEL_TIMEOUT_OVERRIDES",
                "deepseek/deepseek-v3.2=200,qwen/qwen3-max=90",
            )
            cfg_obj = Settings(_env_file=None).as_app_config()  # type: ignore[call-arg]

        overrides = cfg_obj.runtime.llm_per_model_timeout_overrides
        assert overrides == {
            "deepseek/deepseek-v3.2": 200.0,
            "qwen/qwen3-max": 90.0,
        }
