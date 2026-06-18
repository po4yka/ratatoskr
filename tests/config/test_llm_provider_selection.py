from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.adapters.llm.factory import LLMClientFactory
from app.config.runtime import RuntimeConfig


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama", "unknown"])
def test_runtime_rejects_unimplemented_llm_providers(provider: str) -> None:
    with pytest.raises(ValidationError, match="Only 'openrouter' is supported"):
        RuntimeConfig.model_validate({"llm_provider": provider})


def test_runtime_accepts_openrouter_provider_case_insensitively() -> None:
    cfg = RuntimeConfig.model_validate({"llm_provider": " OpenRouter "})
    assert cfg.llm_provider == "openrouter"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
def test_factory_rejects_unimplemented_llm_providers(provider: str) -> None:
    with pytest.raises(ValueError, match="Only 'openrouter' is supported"):
        LLMClientFactory.create(provider, config=object())  # type: ignore[arg-type]
