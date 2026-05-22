"""Tests for LLM client protocol and factory."""

from __future__ import annotations

import inspect

import pytest

from app.adapters.llm.anthropic import AnthropicClient
from app.adapters.llm.openai import OpenAIClient
from app.adapters.llm.protocol import LLMClientProtocol
from app.adapters.openrouter.openrouter_client import OpenRouterClient


class TestLLMClientProtocol:
    """Tests for LLMClientProtocol compliance."""

    def test_openrouter_client_is_protocol_compliant(self) -> None:
        """OpenRouterClient should implement LLMClientProtocol."""
        # Check that OpenRouterClient has all required methods/properties
        assert hasattr(OpenRouterClient, "provider_name")
        assert hasattr(OpenRouterClient, "chat")
        assert hasattr(OpenRouterClient, "aclose")

    def test_openai_client_is_protocol_compliant(self) -> None:
        """OpenAIClient should implement LLMClientProtocol."""
        assert hasattr(OpenAIClient, "provider_name")
        assert hasattr(OpenAIClient, "chat")
        assert hasattr(OpenAIClient, "aclose")

    def test_anthropic_client_is_protocol_compliant(self) -> None:
        """AnthropicClient should implement LLMClientProtocol."""
        assert hasattr(AnthropicClient, "provider_name")
        assert hasattr(AnthropicClient, "chat")
        assert hasattr(AnthropicClient, "aclose")

    @pytest.mark.parametrize(
        "client_cls",
        [OpenRouterClient, OpenAIClient, AnthropicClient],
    )
    def test_chat_signature_matches_generic_workflow_kwargs(self, client_cls: type) -> None:
        """Every provider accepts the kwargs passed by LLMResponseWorkflow."""
        protocol_params = set(inspect.signature(LLMClientProtocol.chat).parameters)
        client_params = set(inspect.signature(client_cls.chat).parameters)

        assert {
            "stream",
            "per_model_timeout_sec",
            "per_model_timeout_overrides",
        } <= protocol_params
        assert protocol_params <= client_params

    def test_provider_names_are_unique(self) -> None:
        """Each provider should have a unique provider name."""
        providers = {
            OpenRouterClient._provider_name,
            OpenAIClient._provider_name,
            AnthropicClient._provider_name,
        }
        assert len(providers) == 3
        assert "openrouter" in providers
        assert "openai" in providers
        assert "anthropic" in providers


class TestOpenAIClient:
    """Tests for OpenAI client initialization."""

    def test_api_key_validation_rejects_empty(self) -> None:
        """Empty API key should raise ValueError."""
        with pytest.raises(ValueError, match="API key is required"):
            OpenAIClient(api_key="")

    def test_api_key_validation_rejects_short(self) -> None:
        """Short API key should raise ValueError."""
        with pytest.raises(ValueError, match="too short"):
            OpenAIClient(api_key="short")

    def test_default_model(self) -> None:
        """Default model should be gpt-4o."""
        client = OpenAIClient(api_key="sk-test-valid-api-key-123456789")
        assert client._model == "gpt-4o"

    def test_provider_name(self) -> None:
        """Provider name should be openai."""
        client = OpenAIClient(api_key="sk-test-valid-api-key-123456789")
        assert client.provider_name == "openai"


class TestAnthropicClient:
    """Tests for Anthropic client initialization."""

    def test_api_key_validation_rejects_empty(self) -> None:
        """Empty API key should raise ValueError."""
        with pytest.raises(ValueError, match="API key is required"):
            AnthropicClient(api_key="")

    def test_api_key_validation_rejects_short(self) -> None:
        """Short API key should raise ValueError."""
        with pytest.raises(ValueError, match="too short"):
            AnthropicClient(api_key="short")

    def test_default_model(self) -> None:
        """Default model should be claude-sonnet-4-5-20250929."""
        client = AnthropicClient(api_key="sk-ant-test-valid-key-123456789")
        assert client._model == "claude-sonnet-4-5-20250929"

    def test_provider_name(self) -> None:
        """Provider name should be anthropic."""
        client = AnthropicClient(api_key="sk-ant-test-valid-key-123456789")
        assert client.provider_name == "anthropic"
