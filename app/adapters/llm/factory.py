"""LLM client factory for creating provider-specific clients.

OpenRouter is the sole production provider. This factory reads
``config.runtime.llm_provider`` (default ``"openrouter"``) and dispatches
accordingly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.llm.protocol import LLMClientProtocol
    from app.config import AppConfig
    from app.utils.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)


class LLMClientFactory:
    """Factory for creating LLM clients based on provider configuration.

    Usage:
        client = LLMClientFactory.create_from_config(config)
        result = await client.chat(messages)
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
            provider: Provider name. Only ``"openrouter"`` is supported. OpenAI,
                Anthropic, and similar upstreams are selected through OpenRouter
                model IDs, not separate provider adapters.
            config: Application configuration.
            circuit_breaker: Optional circuit breaker for fault tolerance.
            audit: Optional audit callback function.

        Returns:
            LLM client implementing LLMClientProtocol.

        Raises:
            ValueError: If the provider is not supported.
        """
        normalized = provider.lower().strip()

        if normalized != "openrouter":
            msg = (
                f"Invalid LLM provider: {provider!r}. Only 'openrouter' is supported. "
                "Use OpenRouter model IDs such as 'openai/...' or 'anthropic/...' "
                "to route to upstream model families."
            )
            raise ValueError(msg)

        logger.info(
            "llm_client_factory_creating",
            extra={"provider": normalized},
        )

        return LLMClientFactory._create_openrouter(config, circuit_breaker, audit)

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
