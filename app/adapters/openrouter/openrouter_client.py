from __future__ import annotations

import os
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, cast

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    import asyncio

    from collections.abc import AsyncGenerator, Callable
    from typing import Self

    from app.adapter_models.llm.llm_models import LLMCallResult
    from app.utils.circuit_breaker import CircuitBreaker, PerModelCircuitBreaker

import httpx

from app.adapters.llm.base_client import BaseLLMClient
from app.adapters.openrouter import client_validation
from app.adapters.openrouter.chat_engine import OpenRouterChatEngine
from app.adapters.openrouter.error_handler import ErrorHandler
from app.adapters.openrouter.exceptions import (
    ClientError,
    ConfigurationError,
    NetworkError,
)
from app.adapters.openrouter.model_capabilities import ModelCapabilities
from app.adapters.openrouter.payload_logger import PayloadLogger
from app.adapters.openrouter.request_builder import RequestBuilder
from app.adapters.openrouter.response_processor import ResponseProcessor
from app.core.async_utils import raise_if_cancelled

logger = get_logger(__name__)


HTTP2_AVAILABLE = find_spec("h2") is not None

if not HTTP2_AVAILABLE:
    logger.warning(
        "HTTP/2 support disabled because the 'h2' package is not installed; falling back to HTTP/1.1"
    )


@dataclass
class OpenRouterClientConfig:
    """Grouped configuration for OpenRouterClient HTTP, structured output, and prompt caching options."""

    # HTTP/retry settings
    timeout_sec: int = 60
    max_retries: int = 3
    backoff_base: float = 0.5
    max_connections: int = 20
    max_keepalive_connections: int = 10
    keepalive_expiry: float = 30.0
    max_response_size_mb: int = 10
    # Structured output settings
    enable_structured_outputs: bool = True
    structured_output_mode: str = "json_schema"
    require_parameters: bool = True
    auto_fallback_structured: bool = True
    # Prompt caching settings
    enable_prompt_caching: bool = True
    prompt_cache_ttl: str = "ephemeral"
    prompt_cache_ttl_anthropic: str = "1h"
    cache_system_prompt: bool = True
    cache_large_content_threshold: int = 4096
    # Transport-layer retry settings (tenacity, network errors only)
    transport_retry_max_attempts: int = 3
    transport_retry_min_wait_sec: float = 0.5
    transport_retry_max_wait_sec: float = 5.0
    # Debug/logging
    debug_payloads: bool = False
    enable_stats: bool = False
    log_truncate_length: int = 1000


class OpenRouterClient:
    """Enhanced OpenRouter Chat Completions client with structured output support.

    This client implements the LLMClientProtocol interface, allowing it to be used
    interchangeably with other LLM providers (OpenAI, Anthropic) in the application.
    """

    # Provider name for protocol compliance
    _provider_name: str = "openrouter"

    # Class-level client pool for connection reuse
    _client_pools: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]
    ] = weakref.WeakKeyDictionary()
    _cleanup_registry: weakref.WeakSet[OpenRouterClient] = weakref.WeakSet()

    # Async lock for client pool access (created lazily per event loop)
    _client_pool_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
        weakref.WeakKeyDictionary()
    )
    # Thread lock to protect async lock initialization (fixes race condition)
    _lock_init_lock = threading.Lock()

    _get_event_loop = BaseLLMClient.__dict__["_get_event_loop"]
    _get_pool_lock = BaseLLMClient.__dict__["_get_pool_lock"]
    _get_pool = BaseLLMClient.__dict__["_get_pool"]

    def __init__(
        self,
        api_key: str,
        config: OpenRouterClientConfig | None = None,
        *,
        model: str,
        fallback_models: list[str] | tuple[str, ...] | None = None,
        http_referer: str | None = None,
        x_title: str | None = None,
        provider_order: list[str] | tuple[str, ...] | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
        circuit_breaker: CircuitBreaker | PerModelCircuitBreaker | None = None,
    ) -> None:
        cfg = config or OpenRouterClientConfig()
        self._validate_init_params(
            api_key,
            model,
            fallback_models,
            http_referer,
            x_title,
            cfg.timeout_sec,
            cfg.max_retries,
            cfg.backoff_base,
            cfg.structured_output_mode,
            cfg.max_response_size_mb,
        )
        self._set_core_configuration(
            api_key=api_key,
            model=model,
            fallback_models=fallback_models,
            timeout_sec=cfg.timeout_sec,
            enable_structured_outputs=cfg.enable_structured_outputs,
            max_response_size_mb=cfg.max_response_size_mb,
            max_connections=cfg.max_connections,
            max_keepalive_connections=cfg.max_keepalive_connections,
            keepalive_expiry=cfg.keepalive_expiry,
            enable_prompt_caching=cfg.enable_prompt_caching,
            prompt_cache_ttl=cfg.prompt_cache_ttl,
            prompt_cache_ttl_anthropic=cfg.prompt_cache_ttl_anthropic,
            cache_system_prompt=cfg.cache_system_prompt,
            cache_large_content_threshold=cfg.cache_large_content_threshold,
        )
        self._transport_retry_max_attempts = cfg.transport_retry_max_attempts
        self._transport_retry_min_wait_sec = cfg.transport_retry_min_wait_sec
        self._transport_retry_max_wait_sec = cfg.transport_retry_max_wait_sec
        self._set_pricing_overrides()
        self._initialize_components(
            api_key=api_key,
            http_referer=http_referer,
            x_title=x_title,
            provider_order=provider_order,
            enable_structured_outputs=cfg.enable_structured_outputs,
            structured_output_mode=cfg.structured_output_mode,
            require_parameters=cfg.require_parameters,
            enable_prompt_caching=cfg.enable_prompt_caching,
            prompt_cache_ttl=cfg.prompt_cache_ttl,
            prompt_cache_ttl_anthropic=cfg.prompt_cache_ttl_anthropic,
            cache_system_prompt=cfg.cache_system_prompt,
            cache_large_content_threshold=cfg.cache_large_content_threshold,
            enable_stats=cfg.enable_stats,
            timeout_sec=cfg.timeout_sec,
            max_retries=cfg.max_retries,
            backoff_base=cfg.backoff_base,
            audit=audit,
            auto_fallback_structured=cfg.auto_fallback_structured,
            debug_payloads=cfg.debug_payloads,
            log_truncate_length=cfg.log_truncate_length,
        )
        self._client_key = (
            f"{self._base_url}:{hash((api_key, cfg.timeout_sec, cfg.max_connections))}"
        )
        self._client: httpx.AsyncClient | None = None
        self._circuit_breaker = circuit_breaker
        self._cleanup_registry.add(self)

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        circuit_breaker: Any | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> OpenRouterClient:
        """Construct from AppConfig, extracting all relevant settings."""
        or_cfg = config.openrouter
        rt_cfg = config.runtime
        client_cfg = OpenRouterClientConfig(
            timeout_sec=rt_cfg.request_timeout_sec,
            debug_payloads=rt_cfg.debug_payloads,
            log_truncate_length=rt_cfg.log_truncate_length,
            enable_stats=or_cfg.enable_stats,
            enable_structured_outputs=or_cfg.enable_structured_outputs,
            structured_output_mode=or_cfg.structured_output_mode,
            require_parameters=or_cfg.require_parameters,
            auto_fallback_structured=or_cfg.auto_fallback_structured,
            max_response_size_mb=or_cfg.max_response_size_mb,
            enable_prompt_caching=or_cfg.enable_prompt_caching,
            prompt_cache_ttl=or_cfg.prompt_cache_ttl,
            prompt_cache_ttl_anthropic=or_cfg.prompt_cache_ttl_anthropic,
            cache_system_prompt=or_cfg.cache_system_prompt,
            cache_large_content_threshold=or_cfg.cache_large_content_threshold,
            transport_retry_max_attempts=or_cfg.transport_retry_max_attempts,
            transport_retry_min_wait_sec=or_cfg.transport_retry_min_wait_sec,
            transport_retry_max_wait_sec=or_cfg.transport_retry_max_wait_sec,
        )
        return cls(
            or_cfg.api_key,
            client_cfg,
            model=or_cfg.model,
            fallback_models=list(or_cfg.fallback_models),
            http_referer=or_cfg.http_referer,
            x_title=or_cfg.x_title,
            provider_order=list(or_cfg.provider_order),
            audit=audit,
            circuit_breaker=circuit_breaker,
        )

    def _set_core_configuration(
        self,
        *,
        api_key: str,
        model: str,
        fallback_models: list[str] | tuple[str, ...] | None,
        timeout_sec: int,
        enable_structured_outputs: bool,
        max_response_size_mb: int,
        max_connections: int,
        max_keepalive_connections: int,
        keepalive_expiry: float,
        enable_prompt_caching: bool,
        prompt_cache_ttl: str,
        prompt_cache_ttl_anthropic: str,
        cache_system_prompt: bool,
        cache_large_content_threshold: int,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._fallback_models = self._validate_fallback_models(fallback_models)
        self._timeout = httpx.Timeout(timeout_sec, connect=10.0, read=timeout_sec)
        self._base_url = "https://openrouter.ai/api/v1"
        self._enable_structured_outputs = enable_structured_outputs
        self._closed = False
        self._oai_client: Any = None
        self._instructor_async_client: Any = None
        self._instructor_init_lock: Any = None  # asyncio.Lock, created lazily
        self._max_response_size_bytes = int(max_response_size_mb) * 1024 * 1024
        self._enable_prompt_caching = enable_prompt_caching
        self._prompt_cache_ttl = prompt_cache_ttl
        self._prompt_cache_ttl_anthropic = prompt_cache_ttl_anthropic
        self._cache_system_prompt = cache_system_prompt
        self._cache_large_content_threshold = cache_large_content_threshold
        self._limits = httpx.Limits(
            max_keepalive_connections=max_keepalive_connections,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        )

    def _set_pricing_overrides(self) -> None:
        self._price_input_per_1k = self._parse_optional_float_env("OPENROUTER_PRICE_INPUT_PER_1K")
        self._price_output_per_1k = self._parse_optional_float_env("OPENROUTER_PRICE_OUTPUT_PER_1K")

    def _parse_optional_float_env(self, key: str) -> float | None:
        try:
            return float(os.getenv(key, ""))
        except Exception:
            return None

    def _initialize_components(
        self,
        *,
        api_key: str,
        http_referer: str | None,
        x_title: str | None,
        provider_order: list[str] | tuple[str, ...] | None,
        enable_structured_outputs: bool,
        structured_output_mode: str,
        require_parameters: bool,
        enable_prompt_caching: bool,
        prompt_cache_ttl: str,
        prompt_cache_ttl_anthropic: str,
        cache_system_prompt: bool,
        cache_large_content_threshold: int,
        enable_stats: bool,
        timeout_sec: int,
        max_retries: int,
        backoff_base: float,
        audit: Callable[[str, str, dict[str, Any]], None] | None,
        auto_fallback_structured: bool,
        debug_payloads: bool,
        log_truncate_length: int,
    ) -> None:
        self.request_builder = self._init_component(
            "request_builder",
            lambda: RequestBuilder(
                api_key=api_key,
                http_referer=http_referer,
                x_title=x_title,
                provider_order=provider_order,
                enable_structured_outputs=enable_structured_outputs,
                structured_output_mode=structured_output_mode,
                require_parameters=require_parameters,
                enable_prompt_caching=enable_prompt_caching,
                prompt_cache_ttl=prompt_cache_ttl,
                prompt_cache_ttl_anthropic=prompt_cache_ttl_anthropic,
                cache_system_prompt=cache_system_prompt,
                cache_large_content_threshold=cache_large_content_threshold,
            ),
        )
        self.response_processor = self._init_component(
            "response_processor", lambda: ResponseProcessor(enable_stats=enable_stats)
        )
        self.model_capabilities = self._init_component(
            "model_capabilities",
            lambda: ModelCapabilities(
                api_key=api_key,
                base_url=self._base_url,
                http_referer=http_referer,
                x_title=x_title,
                timeout=int(timeout_sec),
            ),
        )
        self.error_handler = self._init_component(
            "error_handler",
            lambda: ErrorHandler(
                max_retries=max_retries,
                backoff_base=backoff_base,
                audit=audit,
                auto_fallback_structured=auto_fallback_structured,
            ),
        )
        self.payload_logger = self._init_component(
            "payload_logger",
            lambda: PayloadLogger(
                debug_payloads=debug_payloads,
                log_truncate_length=log_truncate_length,
            ),
        )
        self.chat_engine = self._init_component("chat_engine", lambda: OpenRouterChatEngine(self))

    def _init_component(self, component: str, factory: Callable[[], Any]) -> Any:
        try:
            return factory()
        except Exception as e:
            raise_if_cancelled(e)
            msg = f"Failed to initialize {component.replace('_', ' ')}: {e}"
            raise ConfigurationError(
                msg,
                context={"component": component, "original_error": str(e)},
            ) from e

    @property
    def circuit_breaker(self) -> CircuitBreaker | PerModelCircuitBreaker | None:
        """Return the circuit breaker instance if configured."""
        return self._circuit_breaker

    def get_circuit_breaker_stats(self) -> dict[str, Any]:
        """Get circuit breaker statistics."""
        if self._circuit_breaker is None:
            return {"state": "disabled"}
        from app.utils.circuit_breaker import PerModelCircuitBreaker as _PerModel

        if isinstance(self._circuit_breaker, _PerModel):
            return self._circuit_breaker.all_stats()
        return self._circuit_breaker.get_stats()

    @property
    def provider_name(self) -> str:
        """Return the provider name for LLMClientProtocol compliance."""
        return self._provider_name

    cleanup_all_clients = BaseLLMClient.__dict__["cleanup_all_clients"]

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        """Async context manager exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close pooled HTTP clients associated with the current loop/client key."""
        oai = getattr(self, "_oai_client", None)
        if oai is not None:
            try:
                await oai.close()
            except Exception:
                pass
            self._oai_client = None
            self._instructor_async_client = None
        await BaseLLMClient.aclose(cast("BaseLLMClient", self))

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Ensure an AsyncClient exists for the current event loop and return it."""
        return await BaseLLMClient._ensure_client(cast("BaseLLMClient", self))

    def _get_error_message(self, status_code: int, data: dict[str, Any] | None) -> str:
        return client_validation.get_error_message(status_code, data)

    def _validate_init_params(
        self,
        api_key: str,
        model: str,
        fallback_models: list[str] | tuple[str, ...] | None,
        http_referer: str | None,
        x_title: str | None,
        timeout_sec: int,
        max_retries: int,
        backoff_base: float,
        structured_output_mode: str,
        max_response_size_mb: int,
    ) -> None:
        client_validation.validate_init_params(
            api_key=api_key,
            model=model,
            fallback_models=fallback_models,
            http_referer=http_referer,
            x_title=x_title,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            backoff_base=backoff_base,
            structured_output_mode=structured_output_mode,
            max_response_size_mb=max_response_size_mb,
        )

    def _validate_fallback_models(
        self, fallback_models: list[str] | tuple[str, ...] | None
    ) -> list[str]:
        return client_validation.validate_fallback_models(fallback_models)

    @asynccontextmanager
    async def _request_context(self) -> AsyncGenerator[httpx.AsyncClient]:  # type: ignore[type-arg, unused-ignore]
        """Context manager for request handling with proper error handling."""
        if self._closed:
            msg = "Cannot use client after it has been closed"
            raise ClientError(msg)

        client = await self._ensure_client()

        try:
            yield client
        except httpx.TimeoutException as e:
            msg = f"Request timeout: {e}"
            raise NetworkError(
                msg,
                context={
                    "client": "shared" if client in self._get_pool().values() else "dedicated",
                    "timeout_seconds": (
                        self._timeout.read_timeout
                        if hasattr(self._timeout, "read_timeout")
                        else "unknown"
                    ),
                },
            ) from e
        except httpx.ConnectError as e:
            msg = f"Connection failed: {e}"
            raise NetworkError(
                msg,
                context={
                    "client": "shared" if client in self._get_pool().values() else "dedicated",
                    "base_url": self._base_url,
                },
            ) from e
        except httpx.HTTPStatusError:
            # Don't wrap HTTP errors here - let them be handled by the caller
            # This preserves the original httpx.HTTPStatusError for proper handling
            raise
        except Exception as e:
            raise_if_cancelled(e)
            msg = f"Unexpected client error: {e}"
            raise ClientError(
                msg,
                context={
                    "client": "shared" if client in self._get_pool().values() else "dedicated",
                    "error_type": type(e).__name__,
                },
            ) from e

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
        on_stream_delta: Callable[[str], Any] | None = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> LLMCallResult:
        from app.observability.otel import get_tracer

        tracer = get_tracer(__name__)
        with tracer.start_as_current_span(
            "llm.chat",
            attributes={
                "llm.provider": "openrouter",
                "llm.model": self._model,
            },
        ) as span:
            result = await self.chat_engine.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                stream=stream,
                request_id=request_id,
                response_format=response_format,
                model_override=model_override,
                fallback_models_override=fallback_models_override,
                on_stream_delta=on_stream_delta,
                per_model_timeout_sec=per_model_timeout_sec,
                per_model_timeout_overrides=per_model_timeout_overrides,
                budget_tight_ratio=budget_tight_ratio,
                truncation_max_count=truncation_max_count,
            )
            if hasattr(result, "cost_usd") and result.cost_usd:
                span.set_attribute("llm.cost_usd", result.cost_usd)
            if hasattr(result, "latency_ms") and result.latency_ms:
                span.set_attribute("llm.latency_ms", result.latency_ms)
            return cast("LLMCallResult", result)

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
        """Structured chat completion via Instructor (Pydantic-validated, auto-reask)."""
        from app.observability.otel import get_tracer as _get_tracer

        _tracer = _get_tracer(__name__)
        with _tracer.start_as_current_span(
            "llm.chat_structured",
            attributes={
                "llm.provider": "openrouter",
                "llm.model": self._model,
            },
        ):
            return await self._chat_structured_impl(
                messages,
                response_model=response_model,
                max_retries=max_retries,
                temperature=temperature,
                max_tokens=max_tokens,
                request_id=request_id,
                model_override=model_override,
                fallback_models_override=fallback_models_override,
            )

    async def _chat_structured_impl(
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
        """Inner implementation of chat_structured, called within the OTel span."""
        import time

        import instructor
        from openai import AsyncOpenAI

        from app.adapter_models.llm.llm_models import StructuredLLMResult

        if self._closed:
            msg = "Client has been closed"
            raise RuntimeError(msg)
        if not messages:
            msg = "Messages cannot be empty"
            raise ValueError(msg)

        # Lazy-init the Instructor-wrapped AsyncOpenAI client.  Use a per-instance
        # asyncio.Lock (created lazily to avoid binding to a loop at __init__ time)
        # so that concurrent first-callers don't each construct a client and leak
        # the losers.
        if self._instructor_init_lock is None:
            import asyncio as _asyncio

            self._instructor_init_lock = _asyncio.Lock()
        async with self._instructor_init_lock:
            if self._instructor_async_client is None:
                timeout_sec = float(self._timeout.read or 120)
                self._oai_client = AsyncOpenAI(
                    base_url=self._base_url,
                    api_key=self._api_key,
                    timeout=timeout_sec,
                )
                self._instructor_async_client = instructor.from_openai(
                    self._oai_client,
                    mode=instructor.Mode.JSON,
                )

        primary_model = model_override or self._model
        models_to_try = (
            list(fallback_models_override)
            if fallback_models_override
            else [primary_model] + [m for m in self._fallback_models if m != primary_model]
        )

        last_exc: Exception | None = None
        for model in models_to_try:
            started = time.perf_counter()
            try:
                (
                    parsed,
                    completion,
                ) = await self._instructor_async_client.chat.completions.create_with_completion(
                    model=model,
                    messages=messages,
                    response_model=response_model,
                    max_retries=max_retries,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                latency = int((time.perf_counter() - started) * 1000)
                usage = getattr(completion, "usage", None)
                tokens_prompt = getattr(usage, "prompt_tokens", None)
                tokens_completion = getattr(usage, "completion_tokens", None)
                cost_usd = self._structured_cost_usd(usage, tokens_prompt, tokens_completion)
                logger.debug(
                    "chat_structured_success",
                    extra={
                        "model": model,
                        "latency_ms": latency,
                        "request_id": request_id,
                        "response_model": response_model.__name__,
                        "cost_usd": cost_usd,
                    },
                )
                return StructuredLLMResult(
                    parsed=parsed,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion,
                    cost_usd=cost_usd,
                    latency_ms=latency,
                    model_used=model,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "chat_structured_model_failed",
                    extra={
                        "model": model,
                        "error": str(exc)[:200],
                        "request_id": request_id,
                    },
                )

        raise last_exc or RuntimeError("All models exhausted in chat_structured")

    def _structured_cost_usd(
        self,
        usage: Any,
        tokens_prompt: Any,
        tokens_completion: Any,
    ) -> float | None:
        """Cost for a structured call: provider-reported value, else token x price.

        Mirrors the non-structured path (chat_response_handler) so the instructor
        path no longer records a null cost. Prefers the provider's own cost (which
        OpenRouter attaches to usage when available), falling back to the configured
        per-1k token prices.
        """
        provider_cost: Any = None
        if isinstance(usage, dict):
            provider_cost = usage.get("cost")
        elif usage is not None:
            provider_cost = getattr(usage, "cost", None)
        if isinstance(provider_cost, (int, float)) and not isinstance(provider_cost, bool):
            return float(provider_cost)

        if (
            tokens_prompt is None
            or tokens_completion is None
            or self._price_input_per_1k is None
            or self._price_output_per_1k is None
        ):
            return None
        try:
            return (float(tokens_prompt) / 1000.0) * self._price_input_per_1k + (
                float(tokens_completion) / 1000.0
            ) * self._price_output_per_1k
        except (TypeError, ValueError):
            return None

    async def get_models(self) -> dict[str, Any]:
        """Get available models from OpenRouter API."""
        if self._closed:
            msg = "Client has been closed"
            raise RuntimeError(msg)
        return cast("dict[str, Any]", await self.model_capabilities.get_models())

    async def get_structured_models(self) -> set[str]:
        """Get set of models that support structured outputs."""
        if self._closed:
            msg = "Client has been closed"
            raise RuntimeError(msg)
        return cast("set[str]", await self.model_capabilities.get_structured_models())
