from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.adapters.llm.factory import LLMClientFactory
from app.config import (
    DirectAnthropicConfig,
    DirectOllamaConfig,
    DirectOpenAIConfig,
    clear_config_cache,
    load_config,
)
from app.config.runtime import RuntimeConfig
from tests.conftest import make_test_app_config


@pytest.mark.parametrize("provider", ["openrouter", "openai", "anthropic", "ollama"])
def test_runtime_accepts_supported_llm_providers(provider: str) -> None:
    cfg = RuntimeConfig.model_validate({"llm_provider": f" {provider.upper()} "})
    assert cfg.llm_provider == provider


def test_runtime_rejects_unknown_llm_provider() -> None:
    with pytest.raises(ValidationError, match="Must be one of"):
        RuntimeConfig.model_validate({"llm_provider": "unknown"})


def test_runtime_accepts_openrouter_provider_case_insensitively() -> None:
    cfg = RuntimeConfig.model_validate({"llm_provider": " OpenRouter "})
    assert cfg.llm_provider == "openrouter"


def test_factory_rejects_unknown_llm_provider() -> None:
    with pytest.raises(ValueError, match="Supported providers"):
        LLMClientFactory.create("unknown", config=object())  # type: ignore[arg-type]


def test_factory_builds_openai_provider() -> None:
    cfg = make_test_app_config(
        runtime=RuntimeConfig(llm_provider="openai"),
        openai=DirectOpenAIConfig(api_key="sk-test", model="gpt-4o-mini"),
    )

    client = LLMClientFactory.create_from_config(cfg)

    assert client.provider_name == "openai"


def test_factory_builds_anthropic_provider() -> None:
    cfg = make_test_app_config(
        runtime=RuntimeConfig(llm_provider="anthropic"),
        anthropic=DirectAnthropicConfig(api_key="sk-ant-test", model="claude-sonnet-4-5"),
    )

    client = LLMClientFactory.create_from_config(cfg)

    assert client.provider_name == "anthropic"


def test_factory_builds_ollama_provider_without_api_key() -> None:
    cfg = make_test_app_config(
        runtime=RuntimeConfig(llm_provider="ollama"),
        ollama=DirectOllamaConfig(model="llama3.2"),
    )

    client = LLMClientFactory.create_from_config(cfg)

    assert client.provider_name == "ollama"


def test_direct_provider_startup_does_not_require_openrouter_env(monkeypatch) -> None:
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_FALLBACK_MODELS",
        "OPENROUTER_FLASH_MODEL",
        "OPENROUTER_FLASH_FALLBACK_MODELS",
        "OPENROUTER_LONG_CONTEXT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    clear_config_cache()

    cfg = load_config()

    assert cfg.runtime.llm_provider == "openai"
    assert cfg.openai.api_key == "sk-openai-test"
    assert cfg.openai.model == "gpt-4o-mini"


@pytest.mark.uses_real_yaml
def test_direct_provider_startup_honors_yaml_provider_without_openrouter_env(
    monkeypatch, tmp_path
) -> None:
    yaml_path = tmp_path / "ratatoskr.yaml"
    yaml_path.write_text(
        """
runtime:
  llm_provider: openai
openai:
  model: gpt-4o-mini
""".lstrip(),
        encoding="utf-8",
    )
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_FALLBACK_MODELS",
        "OPENROUTER_FLASH_MODEL",
        "OPENROUTER_FLASH_FALLBACK_MODELS",
        "OPENROUTER_LONG_CONTEXT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("RATATOSKR_CONFIG", str(yaml_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    clear_config_cache()

    cfg = load_config()

    assert cfg.runtime.llm_provider == "openai"
    assert cfg.openai.model == "gpt-4o-mini"
