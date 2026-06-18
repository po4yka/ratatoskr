from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

from app.adapter_models.llm.llm_models import LLMCallResult
from app.adapters.openrouter.chat_attempt_runner import ChatAttemptRunner
from app.adapters.openrouter.chat_context_builder import ChatContextBuilder
from app.adapters.openrouter.chat_models import (
    OpenRouterChatClient,
    StructuredOutputState,
)
from app.adapters.openrouter.chat_response_handler import ChatResponseHandler
from app.adapters.openrouter.chat_streaming import ChatStreamingHandler
from app.adapters.openrouter.chat_transport import ChatTransport
from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger
from app.observability.attributes import (
    LLM_FALLBACK_RUNG_INDEX,
    LLM_MODELS_ATTEMPTED_COUNT,
)
from app.observability.metrics import (
    record_llm_call_retry_exhaustion,
    record_per_model_circuit_breaker_state,
    record_per_model_latency,
    record_per_model_timeout,
)

logger = get_logger(__name__)

_IMAGE_FETCH_ERROR_SIGNALS = (
    "fetching image from url",
    "fetching image",
    "failed to fetch image",
    "invalid image url",
)


def _has_image_fetch_error(
    error_text: str | None,
    error_context: dict[str, Any] | None,
) -> bool:
    haystack = (error_text or "").lower()
    if error_context:
        for key in ("message", "api_error"):
            value = error_context.get(key)
            if isinstance(value, str):
                haystack += " " + value.lower()
    return any(signal in haystack for signal in _IMAGE_FETCH_ERROR_SIGNALS)


def _strip_images_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return messages with image parts removed and a count of stripped images.

    Multimodal user messages have ``content`` as a list of parts; text parts are
    concatenated back into a single string so downstream text-only models can
    still consume them.
    """
    stripped = 0
    new_messages: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            new_messages.append(message)
            continue
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                stripped += 1
                continue
            if part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
        joined = "\n".join(part for part in text_parts if part)
        new_message = dict(message)
        new_message["content"] = joined
        new_messages.append(new_message)
    return new_messages, stripped


@asynccontextmanager
async def _noop_timeout() -> AsyncIterator[None]:
    """No-op async context manager used when per_model_timeout_sec is None."""
    yield


class OpenRouterChatEngine:
    def __init__(self, client: OpenRouterChatClient) -> None:
        self._client = client
        self._context_builder = ChatContextBuilder(client)
        self._response_handler = ChatResponseHandler(client)
        self._streaming_handler = ChatStreamingHandler(self._response_handler)
        self._transport = ChatTransport(client, self._response_handler, self._streaming_handler)
        self._attempt_runner = ChatAttemptRunner(client, self._transport)

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
        per_model_timeout_overrides: Mapping[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> LLMCallResult:
        import time as _time

        if self._client._closed:
            msg = "Client has been closed"
            raise RuntimeError(msg)

        cascade_started = _time.monotonic()

        context = self._context_builder.prepare(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stream=stream,
            request_id=request_id,
            response_format=response_format,
            model_override=model_override,
            fallback_models_override=fallback_models_override,
        )

        current_request = context.request
        structured_output_state = StructuredOutputState()
        last_error_text: str | None = None
        last_data: dict[str, Any] | None = None
        last_latency: int | None = None
        last_model_reported: str | None = None
        last_response_text: str | None = None
        last_error_context: dict[str, Any] | None = None
        images_stripped = False
        models_attempted: list[tuple[str, str]] = []
        per_model_attempts: list[dict[str, Any]] = []

        # Per-model circuit breaker: detect PerModelCircuitBreaker vs legacy global.
        from app.utils.circuit_breaker import PerModelCircuitBreaker as _PerModelCB

        per_model_cb = (
            self._client._circuit_breaker
            if isinstance(self._client._circuit_breaker, _PerModelCB)
            else None
        )
        # Legacy global breaker (kept for backward-compat when caller passes CircuitBreaker).
        global_cb = (
            self._client._circuit_breaker
            if self._client._circuit_breaker is not None and per_model_cb is None
            else None
        )

        # Short-circuit before opening an HTTP context if the legacy global
        # breaker is open. (Per-model breakers are checked per-iteration below.)
        if global_cb is not None and not global_cb.can_proceed():
            return self._circuit_breaker_open_result(request_id)

        try:
            async with self._client._request_context() as http_client:
                for model_index, model in enumerate(context.models_to_try):
                    # --- Per-model circuit breaker check (Improvement A) ---
                    if per_model_cb is not None and not per_model_cb.can_proceed(model):
                        cb_state_str = per_model_cb.state(model).value
                        record_per_model_circuit_breaker_state(model=model, state=cb_state_str)
                        logger.warning(
                            "per_model_circuit_breaker_open",
                            extra={
                                "model": model,
                                "request_id": request_id,
                                "circuit_state": cb_state_str,
                            },
                        )
                        models_attempted.append((model, "circuit_open"))
                        per_model_attempts.append(
                            {
                                "model": model,
                                "status": "circuit_open",
                                "latency_ms": None,
                                "error_text": f"Circuit breaker open for {model}",
                                "error_context": {"circuit_state": cb_state_str},
                                "per_model_timeout_sec": per_model_timeout_sec,
                            }
                        )
                        # Only skip if there are other models whose breakers are closed.
                        remaining = context.models_to_try[model_index + 1 :]
                        if any(per_model_cb.can_proceed(m) for m in remaining):
                            continue
                        # All remaining breakers are also open — fall through to exhausted.
                        break

                    try:
                        # Resolve per-model timeout: explicit override wins, then base floor.
                        effective_timeout: float | None = per_model_timeout_sec
                        if per_model_timeout_overrides:
                            override = per_model_timeout_overrides.get(model)
                            if override is not None:
                                effective_timeout = override
                        model_timeout_cm = (
                            asyncio.timeout(effective_timeout)
                            if effective_timeout is not None
                            else _noop_timeout()
                        )
                        model_start = _time.monotonic()
                        async with model_timeout_cm:
                            (
                                skip_model,
                                structured_output_state,
                            ) = await self._context_builder.maybe_skip_unsupported_structured_model(
                                model=model,
                                primary_model=context.primary_model,
                                response_format=response_format,
                                request_id=request_id,
                                structured_output_state=structured_output_state,
                            )
                            if skip_model:
                                elapsed = _time.monotonic() - model_start
                                models_attempted.append((model, "skipped_unsupported_structured"))
                                per_model_attempts.append(
                                    {
                                        "model": model,
                                        "status": "skipped_unsupported_structured",
                                        "latency_ms": int(elapsed * 1000),
                                        "error_text": "Model does not support structured output; skipped",
                                        "error_context": {"reason": "unsupported_structured"},
                                        "per_model_timeout_sec": effective_timeout,
                                    }
                                )
                                record_per_model_latency(
                                    model=model,
                                    outcome="skipped_unsupported_structured",
                                    seconds=elapsed,
                                )
                                if per_model_cb is not None:
                                    # Structural skip — neither success nor failure.
                                    pass
                                continue

                            model_state = await self._attempt_runner.run_attempts_for_model(
                                client=http_client,
                                model=model,
                                request=current_request,
                                sanitized_messages=context.sanitized_messages,
                                message_lengths=context.message_lengths,
                                message_roles=context.message_roles,
                                total_chars=context.total_chars,
                                request_id=request_id,
                                initial_rf_mode=context.initial_rf_mode,
                                response_format_initial=context.response_format_initial,
                                structured_output_state=structured_output_state,
                                on_stream_delta=on_stream_delta,
                                per_model_timeout_sec=effective_timeout,
                                budget_tight_ratio=budget_tight_ratio,
                                truncation_max_count=truncation_max_count,
                            )
                    except TimeoutError:
                        elapsed = _time.monotonic() - model_start
                        last_model_reported = model
                        last_error_text = (
                            f"Model {model} timed out after {effective_timeout}s.\n"
                            f"Likely cause: OpenRouter latency or model congestion.\n"
                            f"Try resending in a few minutes; if it persists, "
                            f"the content may exceed model context windows."
                        )
                        last_error_context = {
                            "status_code": None,
                            "message": "Per-model timeout",
                            "timeout": True,
                            "model": model,
                            "timeout_sec": effective_timeout,
                        }
                        models_attempted.append((model, "timeout"))
                        per_model_attempts.append(
                            {
                                "model": model,
                                "status": "timeout",
                                "latency_ms": int(elapsed * 1000),
                                "error_text": last_error_text,
                                "error_context": last_error_context,
                                "per_model_timeout_sec": effective_timeout,
                            }
                        )
                        record_per_model_timeout(model=model)
                        record_per_model_latency(model=model, outcome="timeout", seconds=elapsed)
                        if per_model_cb is not None:
                            per_model_cb.record_failure(model)
                            record_per_model_circuit_breaker_state(
                                model=model, state=per_model_cb.state(model).value
                            )
                        logger.warning(
                            "per_model_timeout",
                            extra={
                                "model": model,
                                "request_id": request_id,
                                "timeout_sec": effective_timeout,
                                "models_remaining": len(context.models_to_try) - model_index - 1,
                            },
                        )
                        if model_index < len(context.models_to_try) - 1:
                            self._client.error_handler.log_fallback(
                                model,
                                context.models_to_try[model_index + 1],
                                request_id,
                            )
                        continue

                    current_request = model_state.request
                    structured_output_state = model_state.structured_output_state
                    last_error_text = model_state.last_error_text
                    last_data = model_state.last_data
                    last_latency = model_state.last_latency
                    last_model_reported = model_state.last_model_reported
                    last_response_text = model_state.last_response_text
                    last_error_context = model_state.last_error_context

                    if model_state.terminal_result is not None:
                        outcome = (
                            "success"
                            if getattr(model_state.terminal_result, "status", None) == "ok"
                            else "error"
                        )
                        models_attempted.append((model, outcome))
                        elapsed = _time.monotonic() - model_start
                        record_per_model_latency(model=model, outcome=outcome, seconds=elapsed)
                        if per_model_cb is not None:
                            if outcome == "success":
                                per_model_cb.record_success(model)
                            else:
                                per_model_cb.record_failure(model)
                            record_per_model_circuit_breaker_state(
                                model=model, state=per_model_cb.state(model).value
                            )
                        elif global_cb is not None:
                            if outcome == "success":
                                global_cb.record_success()
                            else:
                                global_cb.record_failure()
                        # Annotate the enclosing llm.chat span with fallback depth.
                        # model_index is 0-based: 0 = primary model answered,
                        # 1+ = a fallback rung was reached.
                        # Use a bare try/except so a missing OTel package never
                        # interrupts the request path.
                        try:
                            from app.observability.otel import _otel_available as _oa

                            if _oa:
                                from opentelemetry import trace as _ot_trace

                                _span = _ot_trace.get_current_span()
                                if _span.is_recording():
                                    _span.set_attribute(LLM_FALLBACK_RUNG_INDEX, model_index)
                                    _span.set_attribute(
                                        LLM_MODELS_ATTEMPTED_COUNT, len(models_attempted)
                                    )
                        except Exception:
                            pass
                        total_latency_ms = max(1, int((_time.monotonic() - cascade_started) * 1000))
                        terminal_status = getattr(model_state.terminal_result, "status", None)
                        terminal_status_value = getattr(terminal_status, "value", terminal_status)
                        terminal_ok = terminal_status_value == CallStatus.OK.value
                        fallback_model_used = None
                        if terminal_ok and model_index > 0:
                            fallback_model_used = (
                                getattr(model_state.terminal_result, "model", None)
                                or last_model_reported
                                or model
                            )
                        return model_state.terminal_result.model_copy(
                            update={
                                "fallback_model_used": fallback_model_used,
                                "retry_exhausted": not terminal_ok,
                                "total_latency_ms": total_latency_ms,
                                "models_attempted": list(models_attempted),
                                "per_model_attempts": [
                                    {
                                        **attempt,
                                        "total_latency_ms": attempt.get("total_latency_ms")
                                        or total_latency_ms,
                                    }
                                    for attempt in per_model_attempts
                                ],
                            }
                        )

                    # Non-terminal: model failed, will try next in ladder.
                    elapsed = _time.monotonic() - model_start
                    models_attempted.append((model, "error"))
                    per_model_attempts.append(
                        {
                            "model": model,
                            "status": "error",
                            "latency_ms": int(elapsed * 1000),
                            "error_text": model_state.last_error_text,
                            "error_context": model_state.last_error_context,
                            "per_model_timeout_sec": effective_timeout,
                        }
                    )
                    record_per_model_latency(model=model, outcome="error", seconds=elapsed)
                    if per_model_cb is not None:
                        per_model_cb.record_failure(model)
                        record_per_model_circuit_breaker_state(
                            model=model, state=per_model_cb.state(model).value
                        )

                    if not images_stripped and _has_image_fetch_error(
                        last_error_text, last_error_context
                    ):
                        stripped_messages, stripped_count = _strip_images_from_messages(
                            context.sanitized_messages
                        )
                        if stripped_count:
                            context.sanitized_messages = stripped_messages
                            context.message_lengths = [
                                len(str(m.get("content", ""))) for m in stripped_messages
                            ]
                            context.message_roles = [m.get("role", "?") for m in stripped_messages]
                            context.total_chars = sum(context.message_lengths)
                            current_request = current_request.model_copy(
                                update={"messages": stripped_messages}
                            )
                            images_stripped = True
                            logger.warning(
                                "openrouter_stripped_images_after_fetch_error",
                                extra={
                                    "model": model,
                                    "request_id": request_id,
                                    "images_removed": stripped_count,
                                    "error_preview": (last_error_text or "")[:200],
                                },
                            )

                    if structured_output_state.parse_error:
                        logger.info(
                            "structured_parse_error_trying_next_model",
                            extra={
                                "model": model,
                                "request_id": request_id,
                                "models_remaining": len(context.models_to_try) - model_index - 1,
                            },
                        )
                    if model_index < len(context.models_to_try) - 1:
                        self._client.error_handler.log_fallback(
                            model,
                            context.models_to_try[model_index + 1],
                            request_id,
                        )
        except Exception as exc:
            raise_if_cancelled(exc)
            last_error_text, last_error_context = self._critical_chat_error_payload(exc)

        self._client.error_handler.log_exhausted(
            context.models_to_try,
            self._client.error_handler._max_retries + 1,
            last_error_text,
            request_id,
        )
        # Increment the retry-exhaustion counter once per request (not per attempt).
        # Uses the last model in the cascade as the label so dashboards can show
        # which model is at the end of the failing chain.
        record_llm_call_retry_exhaustion(
            model=context.models_to_try[-1] if context.models_to_try else "unknown"
        )
        if global_cb is not None:
            global_cb.record_failure()
        total_latency_ms = max(1, int((_time.monotonic() - cascade_started) * 1000))
        return self._attempt_runner.build_exhausted_chat_result(
            last_model_reported=last_model_reported,
            last_response_text=last_response_text,
            last_data=last_data,
            last_latency=last_latency,
            last_error_text=last_error_text,
            last_error_context=last_error_context,
            sanitized_messages=context.sanitized_messages,
            structured_output_state=structured_output_state,
            models_attempted=models_attempted,
            per_model_attempts=[
                {
                    **attempt,
                    "total_latency_ms": attempt.get("total_latency_ms") or total_latency_ms,
                }
                for attempt in per_model_attempts
            ],
            total_latency_ms=total_latency_ms,
        )

    def _circuit_breaker_open_result(self, request_id: int | None) -> LLMCallResult:
        logger.warning(
            "openrouter_circuit_breaker_open",
            extra={
                "request_id": request_id,
                "circuit_state": self._client._circuit_breaker.state.value,
                "failure_count": self._client._circuit_breaker.failure_count,
            },
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
            retry_exhausted=True,
            total_latency_ms=0,
        )

    def _critical_chat_error_payload(self, error: Exception) -> tuple[str, dict[str, Any]]:
        return (
            f"Critical error: {error!s}",
            {
                "status_code": None,
                "message": "Critical client error",
                "api_error": str(error),
                "error_type": "critical",
            },
        )
