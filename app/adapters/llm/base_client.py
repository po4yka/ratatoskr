"""Base LLM client with shared functionality for all providers.

This module contains common HTTP client pooling, retry logic, and error handling
that is shared across all LLM provider implementations.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from contextlib import asynccontextmanager
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

import httpx
from pydantic import BaseModel

from app.adapter_models.llm.llm_models import LLMCallResult, StructuredLLMResult
from app.core.async_utils import raise_if_cancelled
from app.core.backoff import sleep_backoff
from app.core.call_status import CallStatus
from app.core.http_utils import ResponseSizeError, validate_response_size
from app.core.json_utils import extract_json
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from app.utils.circuit_breaker import CircuitBreaker

_ModelT = TypeVar("_ModelT", bound=BaseModel)

logger = get_logger(__name__)

# Check for HTTP/2 support
HTTP2_AVAILABLE = find_spec("h2") is not None

if not HTTP2_AVAILABLE:
    logger.warning(
        "HTTP/2 support disabled because the 'h2' package is not installed; falling back to HTTP/1.1"
    )


class BaseLLMClient:
    """Base class for LLM clients with shared HTTP pooling and retry logic.

    This class provides:
    - Shared HTTP client pooling across instances for connection reuse
    - One structured-call retry loop (``_run_structured_attempts``) with
      exponential backoff, Retry-After support, and per-attempt telemetry
    - Circuit breaker integration
    - Response size validation
    - Proper async resource cleanup

    Subclasses implement the provider-specific ``chat()`` and hand it to
    ``_run_structured_attempts`` from their ``chat_structured()``. Keeping that
    loop here is deliberate: the two direct adapters previously carried their own
    copies, and the copies drifted apart.
    """

    # Class-level client pool for connection reuse across all instances
    _client_pools: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]
    ] = weakref.WeakKeyDictionary()
    _cleanup_registry: weakref.WeakSet[BaseLLMClient] = weakref.WeakSet()

    # Async lock for client pool access (created lazily per event loop)
    _client_pool_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
        weakref.WeakKeyDictionary()
    )
    # Thread lock to protect async lock initialization
    _lock_init_lock = threading.Lock()

    def __init__(
        self,
        *,
        base_url: str,
        timeout_sec: int = 60,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        keepalive_expiry: float = 30.0,
        max_response_size_mb: int = 10,
        circuit_breaker: CircuitBreaker | None = None,
        debug_payloads: bool = False,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
        price_input_per_1k: float | None = None,
        price_output_per_1k: float | None = None,
    ) -> None:
        self._price_input_per_1k = price_input_per_1k
        self._price_output_per_1k = price_output_per_1k
        self._base_url = base_url
        self._timeout = httpx.Timeout(timeout_sec, connect=10.0, read=timeout_sec)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_response_size_bytes = int(max_response_size_mb) * 1024 * 1024
        self._circuit_breaker = circuit_breaker
        self._debug_payloads = debug_payloads
        self._audit = audit
        self._closed = False

        # Connection pool limits
        self._limits = httpx.Limits(
            max_keepalive_connections=max_keepalive_connections,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        )

        # Client management
        self._client_key = f"{self._base_url}:{hash((timeout_sec, max_connections))}"
        self._client: httpx.AsyncClient | None = None

        # Register for cleanup
        self._cleanup_registry.add(self)

    @property
    def circuit_breaker(self) -> CircuitBreaker | None:
        """Return the circuit breaker instance if configured."""
        return self._circuit_breaker

    def _token_cost_usd(
        self,
        tokens_prompt: int | None,
        tokens_completion: int | None,
    ) -> float | None:
        """Price a call from the configured per-1k rates, or None when unpriced.

        Direct providers do not report a USD cost the way OpenRouter does, so
        without these rates every ``llm_calls`` row carried a NULL cost and the
        aggregate budget (``SUM(cost_usd)`` coalesced to 0) could never reach
        ``LLM_DAILY_HARD_BUDGET_USD``. The env-var shape mirrors the existing
        ``OPENROUTER_PRICE_*_PER_1K`` overrides.
        """
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

    def get_circuit_breaker_stats(self) -> dict[str, Any]:
        """Get circuit breaker statistics."""
        if self._circuit_breaker:
            return self._circuit_breaker.get_stats()
        return {"state": "disabled"}

    @classmethod
    def _get_event_loop(cls) -> asyncio.AbstractEventLoop:
        """Return the current event loop (running preferred)."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.get_event_loop()

    @classmethod
    def _get_pool_lock(cls) -> asyncio.Lock:
        """Get or create the async lock for client pool access.

        Thread-safe initialization using double-checked locking pattern.
        """
        loop = cls._get_event_loop()

        # Fast path: lock already exists
        lock = cls._client_pool_locks.get(loop)
        if lock is not None:
            return lock

        # Slow path: create the lock with thread-safe initialization
        with cls._lock_init_lock:
            lock = cls._client_pool_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                cls._client_pool_locks[loop] = lock
            return lock

    @classmethod
    def _get_pool(cls) -> dict[str, httpx.AsyncClient]:
        """Get or create the client pool for the current event loop."""
        loop = cls._get_event_loop()
        pool = cls._client_pools.get(loop)
        if pool is not None:
            return pool

        with cls._lock_init_lock:
            pool = cls._client_pools.get(loop)
            if pool is None:
                pool = {}
                cls._client_pools[loop] = pool
            return pool

    @classmethod
    async def cleanup_all_clients(cls) -> None:
        """Clean up all shared HTTP clients across all instances."""
        with cls._lock_init_lock:
            pools = list(cls._client_pools.values())
            cls._client_pools = weakref.WeakKeyDictionary()
            cls._client_pool_locks = weakref.WeakKeyDictionary()

        clients = [client for pool in pools for client in pool.values()]

        if clients:
            await asyncio.gather(*[client.aclose() for client in clients], return_exceptions=True)

    async def __aenter__(self) -> BaseLLMClient:
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

        # Don't close shared clients, just remove our reference
        if self._client is not None:
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily construct or reuse a pooled AsyncClient instance."""
        if self._closed:
            msg = "Client has been closed"
            raise RuntimeError(msg)

        # Check if we already have a client reference
        if self._client is not None:
            return self._client

        # Use shared client pool for better connection reuse
        async with self._get_pool_lock():
            pool = self._get_pool()
            client = pool.get(self._client_key)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    limits=self._limits,
                    http2=HTTP2_AVAILABLE,
                    follow_redirects=True,
                )
                pool[self._client_key] = client

            self._client = client
            return client

    @asynccontextmanager
    async def _request_context(self) -> AsyncGenerator[httpx.AsyncClient]:  # type: ignore[type-arg, unused-ignore]
        """Context manager for request handling with proper error handling."""
        if self._closed:
            msg = "Cannot use client after it has been closed"
            raise RuntimeError(msg)

        client = await self._ensure_client()

        try:
            yield client
        except httpx.TimeoutException as e:
            msg = f"Request timeout: {e}"
            raise TimeoutError(msg) from e
        except httpx.ConnectError as e:
            msg = f"Connection failed: {e}"
            raise ConnectionError(msg) from e
        except httpx.HTTPStatusError:
            # Let HTTP errors pass through for caller handling
            raise
        except Exception as e:
            raise_if_cancelled(e)
            msg = f"Unexpected client error: {e}"
            raise RuntimeError(msg) from e

    async def _sleep_backoff(self, attempt: int) -> None:
        """Sleep with exponential backoff and jitter."""
        import random

        base_delay = max(0.0, self._backoff_base * (2**attempt))
        jitter = 1.0 + random.uniform(-0.25, 0.25)
        await asyncio.sleep(base_delay * jitter)

    def _check_circuit_breaker(self) -> bool:
        """Check if requests can proceed through the circuit breaker.

        Returns:
            True if requests can proceed, False if circuit is open.
        """
        if self._circuit_breaker and not self._circuit_breaker.can_proceed():
            logger.warning(
                "circuit_breaker_open",
                extra={
                    "provider": getattr(self, "provider_name", "unknown"),
                    "circuit_state": self._circuit_breaker.state.value,
                    "failure_count": self._circuit_breaker.failure_count,
                },
            )
            return False
        return True

    def _record_circuit_breaker_success(self) -> None:
        """Record a successful request with the circuit breaker."""
        if self._circuit_breaker:
            self._circuit_breaker.record_success()

    def _record_circuit_breaker_failure(self) -> None:
        """Record a failed request with the circuit breaker."""
        if self._circuit_breaker:
            self._circuit_breaker.record_failure()

    def _audit_event(self, level: str, event: str, details: dict[str, Any]) -> None:
        """Log an audit event if audit callback is configured."""
        if self._audit:
            self._audit(level, event, details)
        else:
            log_level = logging.INFO if level == "info" else logging.ERROR
            logger.log(log_level, event, extra=details)

    def _extract_error_message(self, data: dict[str, Any]) -> str:
        """Extract error message from API error response."""
        error = data.get("error", {})
        if isinstance(error, dict):
            return cast("str", error.get("message", "Unknown API error"))
        if isinstance(error, str):
            return error
        return cast("str", data.get("message", "Unknown API error"))

    def _parse_http_response(
        self,
        resp: httpx.Response,
        model: str,
        latency: int,
        provider_name: str,
    ) -> tuple[dict[str, Any] | None, LLMCallResult | None]:
        """Validate and parse an HTTP response, returning (data, None) on success.

        Returns (None, error_result) when the response is too large, unparseable, or
        carries a non-200 status code.
        """
        try:
            validate_response_size(resp, self._max_response_size_bytes, provider_name)
        except ResponseSizeError as e:
            return None, LLMCallResult(
                status=CallStatus.ERROR,
                model=model,
                response_text=None,
                error_text=f"Response too large: {e}",
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=None,
                latency_ms=latency,
            )

        try:
            data = resp.json()
        except Exception as e:
            raise_if_cancelled(e)
            return None, LLMCallResult(
                status=CallStatus.ERROR,
                model=model,
                response_text=None,
                error_text=f"Failed to parse JSON response: {e}",
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=None,
                latency_ms=latency,
            )

        if self._debug_payloads:
            logger.debug(
                f"{provider_name.lower()}_response",
                extra={"status": resp.status_code, "latency_ms": latency},
            )

        if resp.status_code != 200:
            error_msg = self._extract_error_message(data)
            error_context: dict[str, Any] = {
                "status_code": resp.status_code,
                "api_error": error_msg,
            }
            # Carry the server's own pacing hint. Without it a 429 gets the
            # generic backoff, which is routinely shorter than the window the
            # provider actually wants and buys another 429.
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                error_context["retry_after"] = retry_after
            return None, LLMCallResult(
                status=CallStatus.ERROR,
                model=model,
                response_text=None,
                response_json=data,
                error_text=error_msg,
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=None,
                latency_ms=latency,
                error_context=error_context,
            )

        return data, None

    # Statuses a retry can never turn into a success: a rotated key stays
    # rotated, an over-long prompt stays over-long, an unknown model stays
    # unknown. Retrying them only multiplies the bill and the latency.
    _NON_RETRYABLE_STATUS: ClassVar[frozenset[int]] = frozenset(
        {400, 401, 402, 403, 404, 405, 410, 413, 422}
    )
    # Cap on how long a provider-supplied Retry-After may park the request, so a
    # bad or hostile header cannot stall the summarize graph indefinitely.
    _RETRY_AFTER_MAX_SEC: ClassVar[float] = 120.0

    async def _run_structured_attempts(
        self,
        *,
        model: str,
        response_model: type[_ModelT],
        max_retries: int,
        call: Callable[[], Awaitable[LLMCallResult]],
    ) -> StructuredLLMResult[_ModelT]:
        """Drive one structured call to a decision, recording every physical request.

        Both direct adapters share this loop on purpose. They used to carry
        byte-identical copies, and the copies drifted: commit 570e7498 added the
        non-retryable-4xx break to the Anthropic loop and left the OpenAI/Ollama
        twin retrying a revoked key four times.

        Every iteration is exactly one billed provider request, so every iteration
        appends one row to ``physical_attempts``. That list is what
        ``graph_llm._physical_attempts`` turns into ``llm_calls`` rows, and it is
        attached to the terminal exception as well -- a failed call is still a call
        that happened, and CLAUDE.md rule 3 requires it in the database.

        Raises:
            RuntimeError: When every attempt fails. Carries
                ``__llm_physical_attempts__`` and ``__llm_result__`` for the
                persist path, and chains the underlying cause.
        """
        # The caller's budget is the re-ask allowance; the provider's own
        # configured ceiling (OPENAI_MAX_RETRIES / ANTHROPIC_MAX_RETRIES /
        # OLLAMA_MAX_RETRIES) bounds it. Before this the provider knob was read
        # from the environment, stored, and never consulted again.
        budget = max(0, min(int(max_retries), int(self._max_retries)))
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        last_result: LLMCallResult | None = None

        if not self._check_circuit_breaker():
            msg = f"{getattr(self, 'provider_name', 'llm')} circuit breaker is open"
            raise RuntimeError(msg)

        for attempt in range(budget + 1):
            started = time.perf_counter()
            try:
                result = await call()
            except (TimeoutError, ConnectionError) as exc:
                # Transport faults are the MOST retryable class there is, yet
                # they used to escape the loop after a single attempt while a
                # hopeless 400 consumed the whole budget.
                last_error = exc
                attempts.append(
                    _attempt_row(
                        model=model,
                        status="error",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error_text=str(exc),
                    )
                )
                if attempt < budget:
                    await sleep_backoff(attempt, backoff_base=self._backoff_base)
                    continue
                break
            except Exception as exc:
                raise_if_cancelled(exc)
                last_error = exc
                attempts.append(
                    _attempt_row(
                        model=model,
                        status="error",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error_text=str(exc),
                    )
                )
                break

            last_result = result
            if result.status != CallStatus.OK or result.response_text is None:
                attempts.append(_attempt_row_from_result(result, error_text=result.error_text))
                last_error = RuntimeError(result.error_text or "Structured chat failed")
                status_code = _status_code_of(result)
                if status_code is not None and status_code in self._NON_RETRYABLE_STATUS:
                    break
                if attempt < budget:
                    await self._wait_before_retry(result, attempt=attempt)
                    continue
                break

            payload = extract_json(result.response_text)
            if payload is None:
                # A 200 whose body is not JSON is still a billed call.
                error_text = "Provider response did not contain a JSON object"
                attempts.append(_attempt_row_from_result(result, error_text=error_text))
                last_error = ValueError(error_text)
                if attempt < budget:
                    await sleep_backoff(attempt, backoff_base=self._backoff_base)
                    continue
                break

            try:
                parsed = response_model.model_validate(payload)
            except Exception as exc:
                raise_if_cancelled(exc)
                attempts.append(_attempt_row_from_result(result, error_text=str(exc)))
                last_error = exc
                if attempt < budget:
                    await sleep_backoff(attempt, backoff_base=self._backoff_base)
                    continue
                break

            attempts.append(_attempt_row_from_result(result, error_text=None))
            self._record_circuit_breaker_success()
            return StructuredLLMResult(
                parsed=parsed,
                tokens_prompt=result.tokens_prompt,
                tokens_completion=result.tokens_completion,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                retry_count=attempt,
                model_used=result.model,
                physical_attempts=attempts,
            )

        self._record_circuit_breaker_failure()
        terminal = RuntimeError(f"Structured chat failed after retries: {last_error}")
        terminal.__llm_physical_attempts__ = attempts  # type: ignore[attr-defined]
        terminal.__llm_result__ = last_result  # type: ignore[attr-defined]
        raise terminal from last_error

    async def _wait_before_retry(self, result: LLMCallResult, *, attempt: int) -> None:
        """Honor the provider's Retry-After hint, falling back to plain backoff."""
        raw = None
        if isinstance(result.error_context, dict):
            raw = result.error_context.get("retry_after")
        if raw is not None:
            try:
                requested = float(raw)
            except (TypeError, ValueError):
                logger.warning("invalid_retry_after_header", extra={"retry_after": str(raw)})
            else:
                capped = min(max(0.0, requested), self._RETRY_AFTER_MAX_SEC)
                if requested > self._RETRY_AFTER_MAX_SEC:
                    logger.warning(
                        "retry_after_header_capped",
                        extra={"requested_seconds": requested, "capped_seconds": capped},
                    )
                await asyncio.sleep(capped)
                return
        await sleep_backoff(attempt, backoff_base=self._backoff_base)


def _attempt_row(
    *,
    model: str | None,
    status: str,
    latency_ms: int | None = None,
    error_text: str | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """One physical provider request, shaped like the OpenRouter reference rows."""
    return {
        "model": model,
        "status": status,
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "error_text": error_text,
    }


def _attempt_row_from_result(result: LLMCallResult, *, error_text: str | None) -> dict[str, Any]:
    """Record a request the provider answered, billed regardless of usability."""
    return _attempt_row(
        model=result.model,
        status="ok" if error_text is None else "error",
        latency_ms=result.latency_ms,
        error_text=error_text,
        tokens_prompt=result.tokens_prompt,
        tokens_completion=result.tokens_completion,
        cost_usd=result.cost_usd,
    )


def _status_code_of(result: LLMCallResult) -> int | None:
    if not isinstance(result.error_context, dict):
        return None
    raw = result.error_context.get("status_code")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def asyncio_sleep_backoff(base: float, attempt: int) -> None:
    """Exponential backoff with light jitter (utility function)."""
    import random

    base_delay = max(0.0, base * (2**attempt))
    jitter = 1.0 + random.uniform(-0.25, 0.25)
    await asyncio.sleep(base_delay * jitter)
