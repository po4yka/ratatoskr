"""Tests for YAML model config loader (load_models_yaml shim in config_file)."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from app.config.config_file import load_models_yaml

if TYPE_CHECKING:
    from pathlib import Path


def _serialize_value(value: object) -> str:
    """Local copy of the serializer used inside load_models_yaml."""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


@pytest.fixture
def yaml_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestLoadModelsYaml:
    def test_missing_file_returns_empty(self, yaml_dir: Path) -> None:
        result = load_models_yaml(yaml_dir / "nonexistent.yaml")
        assert result == {}

    def test_loads_openrouter_model(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                openrouter:
                  model: "anthropic/claude-sonnet-4.6"
            """)
        )
        result = load_models_yaml(cfg)
        assert result["OPENROUTER_MODEL"] == "anthropic/claude-sonnet-4.6"

    def test_loads_fallback_models_as_csv(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                openrouter:
                  fallback_models:
                    - "deepseek/deepseek-v4-flash"
                    - "anthropic/claude-opus-4.6"
            """)
        )
        result = load_models_yaml(cfg)
        assert (
            result["OPENROUTER_FALLBACK_MODELS"]
            == "deepseek/deepseek-v4-flash,anthropic/claude-opus-4.6"
        )

    def test_loads_model_routing(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                model_routing:
                  enabled: true
                  default_model: "test/model"
                  technical_model: "tech/model"
                  long_context_threshold_tokens: 75000
            """)
        )
        result = load_models_yaml(cfg)
        assert result["MODEL_ROUTING_ENABLED"] == "true"
        assert result["MODEL_ROUTING_DEFAULT"] == "test/model"
        assert result["MODEL_ROUTING_TECHNICAL"] == "tech/model"
        assert result["MODEL_ROUTING_LONG_CONTEXT_THRESHOLD_TOKENS"] == "75000"

    def test_loads_multiple_sections(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                openrouter:
                  model: "or/model"
                  temperature: 0.3
                openai:
                  model: "gpt-5"
                anthropic:
                  model: "claude-4"
            """)
        )
        result = load_models_yaml(cfg)
        assert result["OPENROUTER_MODEL"] == "or/model"
        assert result["OPENROUTER_TEMPERATURE"] == "0.3"
        assert result["OPENAI_MODEL"] == "gpt-5"
        assert result["ANTHROPIC_MODEL"] == "claude-4"

    def test_ignores_unknown_sections(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                unknown_section:
                  key: value
                openrouter:
                  model: "test/model"
            """)
        )
        result = load_models_yaml(cfg)
        assert "key" not in result
        assert result["OPENROUTER_MODEL"] == "test/model"

    def test_ignores_unknown_keys_in_section(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                openrouter:
                  model: "test/model"
                  unknown_key: "should be ignored"
            """)
        )
        result = load_models_yaml(cfg)
        assert result == {"OPENROUTER_MODEL": "test/model"}

    def test_invalid_yaml_returns_empty(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text("{{invalid yaml::")
        result = load_models_yaml(cfg)
        assert result == {}

    def test_non_dict_root_returns_empty(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text("- just\n- a\n- list\n")
        result = load_models_yaml(cfg)
        assert result == {}

    def test_empty_file_returns_empty(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text("")
        result = load_models_yaml(cfg)
        assert result == {}

    def test_boolean_serialized_lowercase(self, yaml_dir: Path) -> None:
        cfg = yaml_dir / "models.yaml"
        cfg.write_text(
            dedent("""\
                openrouter:
                  enable_structured_outputs: true
                model_routing:
                  enabled: false
            """)
        )
        result = load_models_yaml(cfg)
        assert result["OPENROUTER_ENABLE_STRUCTURED_OUTPUTS"] == "true"
        assert result["MODEL_ROUTING_ENABLED"] == "false"

    def test_env_path_override(self, yaml_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = yaml_dir / "custom.yaml"
        cfg.write_text(
            dedent("""\
                openrouter:
                  model: "custom/model"
            """)
        )
        monkeypatch.setenv("RATATOSKR_CONFIG", str(cfg))
        result = load_models_yaml()
        assert result["OPENROUTER_MODEL"] == "custom/model"


class TestSerializeValue:
    def test_list_to_csv(self) -> None:
        assert _serialize_value(["a", "b", "c"]) == "a,b,c"

    def test_bool_lowercase(self) -> None:
        assert _serialize_value(True) == "true"
        assert _serialize_value(False) == "false"

    def test_string_passthrough(self) -> None:
        assert _serialize_value("hello") == "hello"

    def test_number_to_string(self) -> None:
        assert _serialize_value(0.2) == "0.2"
        assert _serialize_value(50000) == "50000"
