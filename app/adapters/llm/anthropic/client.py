"""Anthropic LLM client implementation.

This module provides a direct Anthropic API client that implements the LLMClientProtocol.
"""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, cast

import httpx

from app.adapter_models.llm.llm_models import LLMCallResult
from app.adapters.llm.anthropic.request_builder import (
    AnthropicRequestBuilder,
    calculate_cost,
)
from app.adapters.llm.base_client import BaseLLMClient
from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.utils.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)

HTTP2_AVAILABLE = find_spec("h2") is not None


class AnthropicClient:
    """Anthropic Messages API client implementing LLMClientProtocol.

    This client provides direct access to Anthropic's API with:
    - Structured output support via output_format (beta)
    - System prompt extraction to top-level parameter
    - Fallback model chain
    - Circuit breaker integration
    - Connection pooling
    """

    _provider_name: str = "anthropic"

    # Class-level client pool for connection reuse
    _client_pools: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]
    ] = weakref.WeakKeyDictionary()
    _client_pool_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
        weakref.WeakKeyDictionary()
    )
    _lock_init_lock = threading.Lock()

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-sonnet-4-5-20250929",
        fallback_models: list[str] | tuple[str, ...] | None = None,
        timeout_sec: int = 60,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        debug_payloads: bool = False,
        enable_structured_outputs: bool = True,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        keepalive_expiry: float = 30.0,
        max_response_size_mb: int = 10,
        circuit_breaker: CircuitBreaker | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._validate_api_key(api_key)

        self._api_key = api_key
        self._model = model
        self._fallback_models = list(fallback_models) if fallback_models else []
        self._timeout = httpx.Timeout(timeout_sec, connect=10.0, read=timeout_sec)
        self._base_url = "https://api.anthropic.com/v1"
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._debug_payloads = debug_payloads
        self._enable_structured_outputs = enable_structured_outputs
        self._max_response_size_bytes = int(max_response_size_mb) * 1024 * 1024
        self._circuit_breaker = circuit_breaker
        self._audit = audit
        self._closed = False

        # Connection pool limits
        self._limits = httpx.Limits(
            max_keepalive_connections=max_keepalive_connections,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        )

        # Request builder
        self._request_builder = AnthropicRequestBuilder(
            api_key=api_key,
            enable_structured_outputs=enable_structured_outputs,
        )

        # Client management
        self._client_key = f"{self._base_url}:{hash((api_key, timeout_sec, max_connections))}"
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _validate_api_key(api_key: str) -> None:
        """Validate API key format."""
        if not api_key or not isinstance(api_key, str):
            msg = "API key is required and must be a non-empty string"
            raise ValueError(msg)
        if len(api_key.strip()) < 10:
            msg = "API key appears to be invalid (too short)"
            raise ValueError(msg)

    @property
    def provider_name(self) -> str:
        """Return the provider name for LLMClientProtocol compliance."""
        return self._provider_name

    @property
    def circuit_breaker(self) -> CircuitBreaker | None:
        """Return the circuit breaker instance if configured."""
        return self._circuit_breaker

    _get_event_loop = BaseLLMClient.__dict__["_get_event_loop"]
    _get_pool_lock = BaseLLMClient.__dict__["_get_pool_lock"]
    _get_pool = BaseLLMClient.__dict__["_get_pool"]
    _run_with_retry = BaseLLMClient.__dict__["_run_with_retry"]
    _extract_error_message = BaseLLMClient.__dict__["_extract_error_message"]
    _parse_http_response = BaseLLMClient.__dict__["_parse_http_response"]

    async def __aenter__(self) -> AnthropicClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the client and release resources."""
        if self._closed:
            return
        self._closed = True
        self._client = None

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[Any],
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        request_id: int | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
    ) -> Any:
        # Instructor's Anthropic integration requires the anthropic SDK.
        # Install it and replace this stub with instructor.from_anthropic(...)
        # when Anthropic provider support is needed (v2 follow-up).
        msg = (
            f"chat_structured is not yet implemented for the Anthropic provider "
            f"(request_id={request_id}). Use the OpenRouter or OpenAI provider instead."
        )
        raise NotImplementedError(msg)

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily construct or reuse a pooled AsyncClient instance."""
        return await BaseLLMClient._ensure_client(self)  # type: ignore[arg-type]

    _request_context = BaseLLMClient.__dict__["_request_context"]

    async def _sleep_backoff(self, attempt: int) -> None:
        """Sleep with exponential backoff and jitter."""
        import random

        base_delay = max(0.0, self._backoff_base * (2**attempt))
        jitter = 1.0 + random.uniform(-0.25, 0.25)
        await asyncio.sleep(base_delay * jitter)

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
        on_stream_delta: Any | None = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> LLMCallResult:
        """Send a chat completion request to Anthropic.

        Args:
            messages: List of message dictionaries.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            top_p: Nucleus sampling parameter.
            stream: Streaming flag from the generic workflow (ignored for Anthropic adapter).
            request_id: Optional request ID for tracing.
            response_format: Optional structured output format.
            model_override: Optional model override.
            on_stream_delta: Optional stream callback (ignored for Anthropic adapter).

        Returns:
            LLMCallResult with response data.
        """
        if self._closed:
            msg = "Client has been closed"
            raise RuntimeError(msg)

        # Check circuit breaker
        if self._circuit_breaker and not self._circuit_breaker.can_proceed():
            logger.warning(
                "anthropic_circuit_breaker_open",
                extra={"request_id": request_id},
            )
            return LLMCallResult(
                status=CallStatus.ERROR,
                model=None,
                response_text=None,
                error_text="Service temporarily unavailable (circuit breaker open)",
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=0.0,
                latency_ms=0,
            )

        if not messages:
            msg = "Messages cannot be empty"
            raise ValueError(msg)

        if stream or on_stream_delta is not None:
            logger.debug(
                "anthropic_stream_callback_ignored",
                extra={"request_id": request_id, "stream": stream},
            )

        if per_model_timeout_sec is not None or per_model_timeout_overrides:
            logger.debug(
                "anthropic_per_model_timeout_ignored",
                extra={
                    "request_id": request_id,
                    "per_model_timeout_sec": per_model_timeout_sec,
                    "has_overrides": bool(per_model_timeout_overrides),
                },
            )

        # Build model list to try
        primary_model = model_override or self._model
        models_to_try = [primary_model] + [m for m in self._fallback_models if m != primary_model]

        async def _attempt(*, client: httpx.AsyncClient, model: str, attempt: int) -> LLMCallResult:
            return await self._attempt_request(
                client=client,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                response_format=response_format,
                request_id=request_id,
                attempt=attempt,
            )

        return cast(
            "LLMCallResult",
            await self._run_with_retry(
                models_to_try,
                _attempt,
                primary_model=primary_model,
                exhausted_endpoint="/v1/messages",
                retryable_error_substrings=("rate_limit", "overloaded"),
            ),
        )

    async def _attempt_request(
        self,
        client: httpx.AsyncClient,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        top_p: float | None,
        response_format: dict[str, Any] | None,
        request_id: int | None,
        attempt: int,
    ) -> LLMCallResult:
        """Attempt a single request to Anthropic."""
        use_structured = bool(response_format and self._enable_structured_outputs)
        headers = self._request_builder.build_headers(use_structured_outputs=use_structured)
        body = self._request_builder.build_request_body(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            response_format=response_format,
        )

        if self._debug_payloads:
            logger.debug(
                "anthropic_request",
                extra={
                    "model": model,
                    "attempt": attempt,
                    "request_id": request_id,
                    "message_count": len(messages),
                    "has_system": "system" in body,
                },
            )

        started = time.perf_counter()

        try:
            resp = await client.post("/messages", headers=headers, json=body)
        except Exception as e:
            raise_if_cancelled(e)
            latency = int((time.perf_counter() - started) * 1000)
            return LLMCallResult(
                status=CallStatus.ERROR,
                model=model,
                response_text=None,
                error_text=f"Request failed: {e}",
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=None,
                latency_ms=latency,
            )

        latency = int((time.perf_counter() - started) * 1000)

        data, err = self._parse_http_response(resp, model, latency, "Anthropic")
        if err is not None:
            return cast("LLMCallResult", err)

        # Extract successful response
        return self._parse_success_response(data, model, latency, headers, messages, use_structured)

    def _parse_success_response(
        self,
        data: dict[str, Any],
        model: str,
        latency: int,
        headers: dict[str, str],
        messages: list[dict[str, Any]],
        structured_output_used: bool,
    ) -> LLMCallResult:
        """Parse a successful API response.

        Anthropic response format:
        {
            "content": [{"type": "text", "text": "..."}],
            "stop_reason": "end_turn" | "max_tokens" | "stop_sequence",
            "usage": {"input_tokens": ..., "output_tokens": ...}
        }
        """
        # Extract content - Anthropic uses content array with text blocks
        content_blocks = data.get("content", [])
        text_content = ""
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_content += block.get("text", "")

        # Check for truncation
        stop_reason = data.get("stop_reason")
        if stop_reason == "max_tokens":
            logger.warning("anthropic_response_truncated", extra={"model": model})

        # Extract usage
        usage = data.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)

        # Calculate cost
        model_reported = data.get("model", model)
        cost = calculate_cost(model_reported, prompt_tokens, completion_tokens)

        # Redact headers for storage
        redacted_headers = self._request_builder.get_redacted_headers(headers)
        sanitized_messages = self._request_builder.sanitize_messages(messages)

        return LLMCallResult(
            status=CallStatus.OK,
            model=model_reported,
            response_text=text_content,
            response_json=data,
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
            cost_usd=cost,
            latency_ms=latency,
            request_headers=redacted_headers,
            request_messages=sanitized_messages,
            endpoint="/v1/messages",
            structured_output_used=structured_output_used,
        )
