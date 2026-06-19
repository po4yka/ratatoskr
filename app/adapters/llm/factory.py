"""LLM client factory for creating provider-specific clients."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.llm.protocol import LLMClientProtocol
    from app.config import AppConfig
    from app.utils.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)
VALID_PROVIDERS = frozenset({"openrouter", "openai", "anthropic", "ollama"})


class LLMClientFactory:
    """Factory for creating LLM clients based on provider configuration.

    Construct instances through DI/bootstrap code, then call the returned
    client's async chat method.
    """

    @staticmethod
    def create(
        provider: str,
        config: AppConfig,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> LLMClientProtocol:
        """Create an LLM client for the specified provider.

        Args:
            provider: Provider name.
            config: Application configuration.
            circuit_breaker: Optional circuit breaker for fault tolerance.
            audit: Optional audit callback function.

        Returns:
            LLM client implementing LLMClientProtocol.

        Raises:
            ValueError: If the provider is not supported.
        """
        normalized = provider.lower().strip()

        if normalized not in VALID_PROVIDERS:
            msg = f"Invalid LLM provider: {provider!r}. Supported providers: {', '.join(sorted(VALID_PROVIDERS))}."
            raise ValueError(msg)

        logger.info(
            "llm_client_factory_creating",
            extra={"provider": normalized},
        )

        if normalized == "openrouter":
            return LLMClientFactory._create_openrouter(config, circuit_breaker, audit)
        if normalized == "openai":
            return LLMClientFactory._create_openai_compatible(
                provider_name="openai",
                api_key=_require_config_value(config.openai.api_key, "OPENAI_API_KEY"),
                model=_require_config_value(config.openai.model, "OPENAI_MODEL"),
                base_url=config.openai.base_url,
                temperature=config.openai.temperature,
                max_tokens=config.openai.max_tokens,
                timeout_sec=config.openai.timeout_sec,
                max_retries=config.openai.max_retries,
                max_response_size_mb=config.openai.max_response_size_mb,
                circuit_breaker=circuit_breaker,
                audit=audit,
            )
        if normalized == "ollama":
            return LLMClientFactory._create_openai_compatible(
                provider_name="ollama",
                api_key=config.ollama.api_key,
                model=_require_config_value(config.ollama.model, "OLLAMA_MODEL"),
                base_url=config.ollama.base_url,
                temperature=config.ollama.temperature,
                max_tokens=config.ollama.max_tokens,
                timeout_sec=config.ollama.timeout_sec,
                max_retries=config.ollama.max_retries,
                max_response_size_mb=config.ollama.max_response_size_mb,
                circuit_breaker=circuit_breaker,
                audit=audit,
            )
        return LLMClientFactory._create_anthropic(config, circuit_breaker, audit)

    @staticmethod
    def _create_openrouter(
        config: AppConfig,
        circuit_breaker: CircuitBreaker | None,
        audit: Callable[[str, str, dict[str, Any]], None] | None,
    ) -> LLMClientProtocol:
        """Create an OpenRouter client."""
        from app.adapters.openrouter.openrouter_client import OpenRouterClient

        return OpenRouterClient.from_config(config, circuit_breaker=circuit_breaker, audit=audit)

    @staticmethod
    def _create_openai_compatible(
        *,
        provider_name: str,
        api_key: str | None,
        model: str,
        base_url: str,
        temperature: float,
        max_tokens: int | None,
        timeout_sec: int,
        max_retries: int,
        max_response_size_mb: int,
        circuit_breaker: CircuitBreaker | None,
        audit: Callable[[str, str, dict[str, Any]], None] | None,
    ) -> LLMClientProtocol:
        from app.adapters.llm.openai_compatible import OpenAICompatibleLLMClient

        return OpenAICompatibleLLMClient(
            provider_name=provider_name,
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            max_response_size_mb=max_response_size_mb,
            circuit_breaker=circuit_breaker,
            audit=audit,
        )

    @staticmethod
    def _create_anthropic(
        config: AppConfig,
        circuit_breaker: CircuitBreaker | None,
        audit: Callable[[str, str, dict[str, Any]], None] | None,
    ) -> LLMClientProtocol:
        from app.adapters.llm.anthropic_direct import AnthropicDirectLLMClient

        return AnthropicDirectLLMClient(
            api_key=_require_config_value(config.anthropic.api_key, "ANTHROPIC_API_KEY"),
            model=_require_config_value(config.anthropic.model, "ANTHROPIC_MODEL"),
            base_url=config.anthropic.base_url,
            version=config.anthropic.version,
            temperature=config.anthropic.temperature,
            max_tokens=config.anthropic.max_tokens,
            timeout_sec=config.anthropic.timeout_sec,
            max_retries=config.anthropic.max_retries,
            max_response_size_mb=config.anthropic.max_response_size_mb,
            circuit_breaker=circuit_breaker,
            audit=audit,
        )

    @staticmethod
    def get_provider_from_config(config: AppConfig) -> str:
        """Get the LLM provider from configuration.

        Args:
            config: Application configuration.

        Returns:
            Provider name string (defaults to ``"openrouter"`` when unset).
        """
        return getattr(config.runtime, "llm_provider", "openrouter")

    @staticmethod
    def create_from_config(
        config: AppConfig,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> LLMClientProtocol:
        """Create an LLM client using the provider specified in config.

        Args:
            config: Application configuration.
            circuit_breaker: Optional circuit breaker for fault tolerance.
            audit: Optional audit callback function.

        Returns:
            LLM client implementing LLMClientProtocol.
        """
        provider = LLMClientFactory.get_provider_from_config(config)
        return LLMClientFactory.create(
            provider, config, circuit_breaker=circuit_breaker, audit=audit
        )


def _require_config_value(value: str | None, env_name: str) -> str:
    if value:
        return value
    msg = f"{env_name} is required when the selected LLM provider uses it"
    raise ValueError(msg)
