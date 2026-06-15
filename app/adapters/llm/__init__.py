"""LLM client abstraction layer.

This module provides a unified interface for interacting with the OpenRouter
LLM provider through a common protocol.

Key components:
- LLMClientProtocol: Abstract interface for all LLM clients
- LLMClientFactory: Factory for creating the OpenRouter client
- BaseLLMClient: Shared functionality (HTTP pooling, retry, circuit breaker)
"""

from app.adapters.llm.base_client import BaseLLMClient, asyncio_sleep_backoff
from app.adapters.llm.factory import LLMClientFactory
from app.adapters.llm.protocol import LLMClientProtocol

__all__ = [
    "BaseLLMClient",
    "LLMClientFactory",
    "LLMClientProtocol",
    "asyncio_sleep_backoff",
]
