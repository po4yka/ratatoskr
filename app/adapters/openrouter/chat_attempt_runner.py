from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.adapter_models.llm.llm_models import ChatRequest, LLMCallResult
from app.adapters.openrouter.chat_models import (
    AttemptOutcome,
    ModelRunState,
    OpenRouterChatClient,
    StructuredOutputState,
)
from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.openrouter.chat_transport import ChatTransport

logger = get_logger(__name__)

# Network-class exceptions that are safe to retry transparently.
# These indicate the connection never reached the server (or the server closed
# it before sending a response), so retrying the identical request is safe.
# Non-network errors (e.g. 4xx/5xx HTTP responses, JSON parse failures) are
# handled by the state-machine loop in run_attempts_for_model and must NOT be
# caught here so they can drive request-shape mutations.
_TRANSPORT_RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)


class ChatAttemptRunner:
    def __init__(self, client: OpenRouterChatClient, transport: ChatTransport) -> None:
        self._client = client
        self._transport = transport

    async def _attempt_transport(
        self,
        http_client: httpx.AsyncClient,
        *,
        model: str,
        attempt: int,
        sanitized_messages: list[dict[str, Any]],
        request: ChatRequest,
        rf_mode_current: str | None,
        response_format_current: dict[str, Any] | None,
        message_lengths: list[int],
        message_roles: list[str],
        total_chars: int,
        request_id: int | None,
        on_stream_delta: Any | None,
    ) -> AttemptOutcome:
        """Call _transport.attempt_request with tenacity retries on network errors.

        Only httpx connection/timeout errors are retried here.  All other
        exceptions (HTTP error responses, parse failures, CancelledError) are
        re-raised immediately so the state-machine loop can handle them.
        """
        max_attempts = getattr(self._client, "_transport_retry_max_attempts", 3)
        min_wait = getattr(self._client, "_transport_retry_min_wait_sec", 0.5)
        max_wait = getattr(self._client, "_transport_retry_max_wait_sec", 5.0)

        async for tenacity_attempt in AsyncRetrying(
            retry=retry_if_exception_type(_TRANSPORT_RETRY_EXCEPTIONS),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            reraise=True,
        ):
            with tenacity_attempt:
                return await self._transport.attempt_request(
                    http_client,
                    model=model,
                    attempt=attempt,
                    sanitized_messages=sanitized_messages,
                    request=request,
                    rf_mode_current=rf_mode_current,
                    response_format_current=response_format_current,
                    message_lengths=message_lengths,
                    message_roles=message_roles,
                    total_chars=total_chars,
                    request_id=request_id,
                    on_stream_delta=on_stream_delta,
                )

        # AsyncRetrying with reraise=True will raise on exhaustion; this line
        # is unreachable but satisfies the type checker.
        raise RuntimeError("unreachable")  # pragma: no cover

    async def run_attempts_for_model(
        self,
        *,
        client: httpx.AsyncClient,
        model: str,
        request: ChatRequest,
        sanitized_messages: list[dict[str, Any]],
        message_lengths: list[int],
        message_roles: list[str],
        total_chars: int,
        request_id: int | None,
        initial_rf_mode: str | None,
        response_format_initial: dict[str, Any] | None,
        structured_output_state: StructuredOutputState,
        on_stream_delta: Any | None = None,
        per_model_timeout_sec: float | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> ModelRunState:
        rf_mode_current = initial_rf_mode
        response_format_current = response_format_initial
        truncation_count = 0
        model_started = time.monotonic()
        state = ModelRunState(
            request=request,
            structured_output_state=structured_output_state,
        )

        for attempt in range(self._client.error_handler._max_retries + 1):
            try:
                outcome = await self._attempt_transport(
                    client,
                    model=model,
                    attempt=attempt,
                    sanitized_messages=sanitized_messages,
                    request=state.request,
                    rf_mode_current=rf_mode_current,
                    response_format_current=response_format_current,
                    message_lengths=message_lengths,
                    message_roles=message_roles,
                    total_chars=total_chars,
                    request_id=request_id,
                    on_stream_delta=on_stream_delta,
                )
            except Exception as exc:
                raise_if_cancelled(exc)
                state.last_error_text = f"Unexpected error: {exc!s}"
                state.last_error_context = {
                    "status_code": None,
                    "message": "Client exception",
                    "api_error": str(exc),
                }
                if attempt < self._client.error_handler._max_retries:
                    await self._client.error_handler.sleep_backoff(attempt)
                    continue
                break

            if outcome.structured_output_state is not None:
                state.structured_output_state = outcome.structured_output_state

            if outcome.success and outcome.llm_result is not None:
                state.terminal_result = outcome.llm_result
                return state

            if outcome.should_retry and outcome.retry is not None:
                if outcome.retry.fallback_to_non_stream and state.request.stream:
                    logger.warning(
                        "openrouter_stream_fallback_non_stream",
                        extra={
                            "model": model,
                            "attempt": attempt + 1,
                            "request_id": request_id,
                        },
                    )
                    from app.observability.metrics import (
                        record_openrouter_stream_fallback,
                    )

                    _fallback_reason = (
                        "stream_request_failed"
                        if (outcome.error_text or "").startswith("stream_request_failed")
                        else "non_streaming_chunk_path"
                    )
                    record_openrouter_stream_fallback(model=model, reason=_fallback_reason)
                    state.request = self._copy_request_with_stream(state.request, False)
                    rf_mode_current = outcome.retry.rf_mode
                    response_format_current = outcome.retry.response_format
                    continue

                rf_mode_current = outcome.retry.rf_mode
                response_format_current = outcome.retry.response_format

                if outcome.retry.truncation_recovery is not None:
                    # Budget-tight guard (Improvement C): skip truncation retry
                    # when more than 60% of the per-model time budget is consumed,
                    # as the next attempt is unlikely to finish in time.
                    if per_model_timeout_sec is not None:
                        elapsed = time.monotonic() - model_started
                        if elapsed / per_model_timeout_sec > budget_tight_ratio:
                            logger.warning(
                                "truncation_recovery_skipped_budget_tight",
                                extra={
                                    "model": model,
                                    "elapsed": elapsed,
                                    "per_model_timeout_sec": per_model_timeout_sec,
                                    "request_id": request_id,
                                },
                            )
                            state.last_error_text = "truncation_recovery_skipped_budget_tight"
                            state.last_error_context = {"reason": "budget_tight"}
                            break

                    truncation_count += 1
                    if truncation_count >= truncation_max_count:
                        logger.warning(
                            "truncation_limit_reached",
                            extra={
                                "model": model,
                                "count": truncation_count,
                                "request_id": request_id,
                            },
                        )
                        state.last_error_text = "repeated_truncation"
                        state.last_error_context = {
                            "status_code": None,
                            "message": "Repeated truncation - trying next model",
                            "truncation_count": truncation_count,
                        }
                        break

                    new_max_tokens = outcome.retry.truncation_recovery.suggested_max_tokens
                    if new_max_tokens and (
                        not state.request.max_tokens or new_max_tokens > state.request.max_tokens
                    ):
                        logger.info(
                            "truncation_recovery_increasing_max_tokens",
                            extra={
                                "model": model,
                                "original_max": state.request.max_tokens,
                                "new_max": new_max_tokens,
                                "attempt": attempt + 1,
                                "truncation_count": truncation_count,
                            },
                        )
                        state.request = self._copy_request_with_max_tokens(
                            state.request,
                            new_max_tokens,
                        )

                if outcome.retry.backoff_needed:
                    await self._client.error_handler.sleep_backoff(attempt)
                continue

            state.last_error_text = outcome.error_text
            state.last_data = outcome.data
            state.last_latency = outcome.latency
            state.last_model_reported = outcome.model_reported
            state.last_response_text = outcome.response_text
            state.last_error_context = outcome.error_context
            if outcome.error_result is not None:
                state.terminal_result = outcome.error_result
                return state
            if outcome.should_try_next_model:
                break

        return state

    def build_exhausted_chat_result(
        self,
        *,
        last_model_reported: str | None,
        last_response_text: str | None,
        last_data: dict[str, Any] | None,
        last_latency: int | None,
        last_error_text: str | None,
        last_error_context: dict[str, Any] | None,
        sanitized_messages: list[dict[str, Any]],
        structured_output_state: StructuredOutputState,
        models_attempted: list[tuple[str, str]] | None = None,
        per_model_attempts: list[dict[str, Any]] | None = None,
    ) -> LLMCallResult:
        redacted_headers = self._client.request_builder.get_redacted_headers(
            {"Authorization": "REDACTED", "Content-Type": "application/json"}
        )
        return LLMCallResult(
            status=CallStatus.ERROR,
            model=last_model_reported,
            response_text=last_response_text,
            response_json=last_data,
            openrouter_response_text=last_response_text,
            openrouter_response_json=last_data,
            tokens_prompt=None,
            tokens_completion=None,
            cost_usd=None,
            latency_ms=last_latency,
            error_text=(
                "structured_output_parse_error"
                if structured_output_state.parse_error
                else (last_error_text or "All retries and fallbacks exhausted")
            ),
            request_headers=redacted_headers,
            request_messages=sanitized_messages,
            endpoint="/api/v1/chat/completions",
            structured_output_used=structured_output_state.used,
            structured_output_mode=structured_output_state.mode,
            error_context=last_error_context,
            models_attempted=models_attempted or [],
            per_model_attempts=per_model_attempts or [],
        )

    def _copy_request_with_max_tokens(self, request: ChatRequest, max_tokens: int) -> ChatRequest:
        return ChatRequest(
            messages=request.messages,
            temperature=request.temperature,
            max_tokens=max_tokens,
            top_p=request.top_p,
            stream=request.stream,
            request_id=request.request_id,
            response_format=request.response_format,
            model_override=request.model_override,
        )

    def _copy_request_with_stream(self, request: ChatRequest, stream: bool) -> ChatRequest:
        return ChatRequest(
            messages=request.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            top_p=request.top_p,
            stream=stream,
            request_id=request.request_id,
            response_format=request.response_format,
            model_override=request.model_override,
        )
