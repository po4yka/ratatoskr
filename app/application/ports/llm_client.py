"""LLM client port for the application layer.

This module defines the canonical LLM client Protocol that application services
depend on. The concrete implementation lives in ``app.adapters.llm``, which
satisfies this protocol structurally.

``app.adapters.llm.protocol`` re-exports ``LLMClientProtocol`` from here so
that adapter-side code that already imports from ``app.adapters.llm.protocol``
continues to work unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapter_models.llm.llm_models import LLMCallResult, StructuredLLMResult

_ModelT = TypeVar("_ModelT", bound=BaseModel)


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Protocol defining the interface for LLM clients.

    All LLM provider implementations (OpenRouter, OpenAI, Anthropic) must
    implement this protocol to be used interchangeably in the application.

    The protocol focuses on the core chat completions functionality that
    all providers support, abstracting away provider-specific details.
    """

    @property
    def provider_name(self) -> str:
        """Return the name of the LLM provider.

        Returns:
            Provider identifier string (e.g., "openrouter", "openai", "anthropic")
        """
        ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stream: bool = False,
        request_id: int | None = None,
        response_format: dict[str, Any] | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
        on_stream_delta: Callable[[str], Awaitable[None] | None] | None = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> LLMCallResult:
        """Send a chat completion request to the LLM provider.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
                     Roles are typically 'system', 'user', or 'assistant'.
            temperature: Sampling temperature (0.0 to 2.0). Lower values make
                        output more deterministic.
            max_tokens: Maximum number of tokens to generate. If None, uses
                       provider defaults.
            top_p: Nucleus sampling parameter (0.0 to 1.0). If None, uses
                  provider defaults.
            stream: Whether to request streaming output when the provider supports it.
            request_id: Optional internal request ID for tracing and persistence.
            response_format: Optional structured output format specification.
                           Provider-specific handling applies.
            model_override: Optional model name to use instead of the default.
            fallback_models_override: Optional ordered list of fallback models.
            on_stream_delta: Optional callback invoked with streamed text deltas.
            per_model_timeout_sec: Optional timeout budget for each model attempt.
            per_model_timeout_overrides: Optional per-model timeout overrides.
            budget_tight_ratio: Fraction of the per-model timeout below which the
                client treats the budget as "tight" and trims optional payload
                (e.g. drops streaming, applies harsher truncation).
            truncation_max_count: Maximum number of message-history truncation
                rounds permitted before failing fast.

        Returns:
            LLMCallResult containing the response text, token usage, cost,
            latency, and error information. API-level failures (rate limits,
            timeouts, model errors, empty responses) are captured in
            ``result.error_text`` with ``result.status = CallStatus.ERROR``.
            Callers must check ``result.status`` — no exception is raised for
            recoverable API errors. Only structural failures raise exceptions.

        Raises:
            RuntimeError: If the client has been closed or all fallback models
                are exhausted without a successful response.
        """
        ...

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[_ModelT],
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        request_id: int | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
    ) -> StructuredLLMResult[_ModelT]:
        """Send a chat completion and parse the response into a Pydantic model.

        Uses Instructor to validate the LLM's JSON output against response_model,
        automatically reask-ing the model (up to max_retries times) when the output
        fails validation. Replaces manual JSON repair + contract validation loops.

        Does NOT support streaming; use chat() with on_stream_delta for streamed output.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
            response_model: Pydantic model class to validate the response against.
            max_retries: Reask attempts on validation failure (default 3).
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            request_id: Optional internal request ID for tracing.
            model_override: Optional model name override.
            fallback_models_override: Optional ordered list of models to try.

        Returns:
            StructuredLLMResult with the validated model instance and usage stats.

        Raises:
            instructor.exceptions.InstructorRetryException: After max_retries failures.
            RuntimeError: If the client has been closed or all models are exhausted.
        """
        ...

    async def aclose(self) -> None:
        """Close the client and release any resources.

        This method should be called when the client is no longer needed
        to properly clean up HTTP connections and other resources.

        After calling aclose(), the client should not be used for further
        requests. Calling chat() after aclose() should raise RuntimeError.
        """
        ...


__all__ = ["LLMClientProtocol"]
