from __future__ import annotations

from typing import Any

from app.adapter_models.llm.llm_models import LLMCallResult
from app.adapters.openrouter.chat_models import (
    AttemptOutcome,
    AttemptRequestPayload,
    OpenRouterChatClient,
    RetryDirective,
    StructuredOutputState,
    TruncationRecovery,
)
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


class ChatResponseHandler:
    def __init__(self, client: OpenRouterChatClient) -> None:
        self._client = client

    def handle_successful_response(
        self,
        *,
        data: dict[str, Any],
        payload: AttemptRequestPayload,
        model: str,
        model_reported: str,
        latency: int,
        attempt: int,
        request_id: int | None,
        sanitized_messages: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> AttemptOutcome:
        logger.debug(
            "processing_successful_response",
            extra={
                "model": model,
                "latency_ms": latency,
                "request_id": request_id,
                "rf_mode": payload.rf_mode_current,
                "stage": "entry",
            },
        )
        text, usage, cost_usd = self._client.response_processor.extract_response_data(
            data, payload.rf_included
        )

        logger.debug(
            "processing_successful_response",
            extra={
                "request_id": request_id,
                "stage": "checking_truncation",
                "text_len": len(text) if text else 0,
            },
        )

        truncated, truncated_finish, truncated_native = (
            self._client.response_processor.inspect_completion_truncation(data)
        )
        if truncated:
            self._client.error_handler.log_truncated_completion(
                model,
                truncated_finish,
                truncated_native,
                request_id,
            )
            return self._build_truncation_outcome(
                payload=payload,
                attempt=attempt,
                text=text,
                max_tokens=max_tokens,
            )

        if payload.rf_included and payload.response_format_current:
            logger.debug(
                "processing_successful_response",
                extra={
                    "request_id": request_id,
                    "stage": "validating_structured_output",
                    "rf_mode": payload.rf_mode_current,
                },
            )
            text, validation_outcome = self._validate_structured_success_payload(
                text=text,
                payload=payload,
                attempt=attempt,
                model=model,
            )
            if validation_outcome is not None:
                return validation_outcome

        finish_reason, native_finish_reason = self._extract_finish_reason(data)
        tokens_prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        tokens_completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
        tokens_total = usage.get("total_tokens") if isinstance(usage, dict) else None

        cache_metrics = self._client.response_processor.extract_cache_metrics(data)
        if cache_metrics.cache_hit or cache_metrics.cache_creation_tokens > 0:
            logger.info(
                "prompt_cache_metrics",
                extra={
                    "model": model_reported,
                    "cache_read_tokens": cache_metrics.cache_read_tokens,
                    "cache_creation_tokens": cache_metrics.cache_creation_tokens,
                    "cache_discount": cache_metrics.cache_discount,
                    "cache_hit": cache_metrics.cache_hit,
                    "request_id": request_id,
                },
            )

        cost_usd = self._estimate_cost_if_missing(
            cost_usd=cost_usd,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
        )
        self._client.payload_logger.log_response(
            status=200,
            latency_ms=latency,
            model=model_reported,
            attempt=attempt,
            request_id=request_id,
            truncated=truncated,
            finish_reason=finish_reason,
            native_finish_reason=native_finish_reason,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_total=tokens_total,
            cost_usd=cost_usd,
            structured_output=payload.rf_included,
            rf_mode=payload.rf_mode_current,
        )
        self._client.error_handler.log_success(
            attempt,
            model,
            200,
            latency,
            payload.structured_output_state.used,
            payload.structured_output_state.mode,
            request_id,
        )
        redacted_headers = self._client.request_builder.get_redacted_headers(payload.headers)
        llm_result = LLMCallResult(
            status=CallStatus.OK,
            model=model_reported,
            response_text=text,
            response_json=data,
            openrouter_response_text=text,
            openrouter_response_json=data,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            cost_usd=cost_usd,
            latency_ms=latency,
            error_text=None,
            request_headers=redacted_headers,
            request_messages=sanitized_messages,
            endpoint="/api/v1/chat/completions",
            structured_output_used=payload.structured_output_state.used,
            structured_output_mode=payload.structured_output_state.mode,
            cache_read_tokens=(
                cache_metrics.cache_read_tokens if cache_metrics.cache_read_tokens > 0 else None
            ),
            cache_creation_tokens=(
                cache_metrics.cache_creation_tokens
                if cache_metrics.cache_creation_tokens > 0
                else None
            ),
            cache_discount=cache_metrics.cache_discount,
        )
        return AttemptOutcome(
            success=True,
            llm_result=llm_result,
            structured_output_state=payload.structured_output_state,
        )

    async def handle_error_response(
        self,
        *,
        status_code: int,
        data: dict[str, Any],
        resp: Any,
        payload: AttemptRequestPayload,
        model: str,
        model_reported: str,
        latency: int,
        attempt: int,
        request_id: int | None,
        sanitized_messages: list[dict[str, Any]],
    ) -> AttemptOutcome:
        downgrade_outcome = self._maybe_downgrade_on_response_format_error(
            status_code=status_code,
            data=data,
            payload=payload,
            attempt=attempt,
            model=model,
            request_id=request_id,
        )
        if downgrade_outcome is not None:
            return downgrade_outcome

        text, usage, _ = self._client.response_processor.extract_response_data(
            data, payload.rf_included
        )
        error_context = self._client.response_processor.get_error_context(status_code, data)
        error_message = str(error_context["message"])
        redacted_headers = self._client.request_builder.get_redacted_headers(payload.headers)

        if self._client.error_handler.is_non_retryable_error(status_code):
            error_message = self._client._get_error_message(status_code, data)
            # Provider-specific content policy errors (e.g. Anthropic robots.txt
            # blocking) should trigger model fallback, not a terminal failure.
            if self._client.error_handler.is_provider_specific_rejection(data):
                self._client.error_handler.log_error(
                    attempt, model, status_code, error_message, request_id, "WARN"
                )
                return AttemptOutcome(
                    error_text=error_message,
                    data=data,
                    latency=latency,
                    model_reported=model_reported,
                    error_context=error_context,
                    should_try_next_model=True,
                    structured_output_state=payload.structured_output_state,
                )
            return AttemptOutcome(
                error_result=self._client.error_handler.build_error_result(
                    model_reported,
                    text,
                    data,
                    usage,
                    latency,
                    error_message,
                    redacted_headers,
                    sanitized_messages,
                    error_context=error_context,
                    retry_exhausted=True,
                ),
                structured_output_state=payload.structured_output_state,
            )

        if self._client.error_handler.should_try_next_model(status_code, error_message):
            structured_downgrade = self._maybe_downgrade_on_endpoint_capability_error(
                status_code=status_code,
                error_message=error_message,
                error_context=error_context,
                payload=payload,
                model=model,
                request_id=request_id,
            )
            if structured_downgrade is not None:
                return structured_downgrade

            self._client.error_handler.log_error(
                attempt,
                model,
                status_code,
                error_message,
                request_id,
                "WARN",
            )
            return AttemptOutcome(
                error_text=error_message,
                data=data,
                latency=latency,
                model_reported=model_reported,
                error_context=error_context,
                should_try_next_model=True,
                structured_output_state=payload.structured_output_state,
            )

        if self._client.error_handler.should_retry(status_code, attempt):
            if status_code == 429:
                await self._client.error_handler.handle_rate_limit(resp.headers)
            return AttemptOutcome(
                error_text=error_message,
                error_context=error_context,
                retry=RetryDirective(
                    rf_mode=payload.rf_mode_current,
                    response_format=payload.response_format_current,
                    backoff_needed=status_code != 429,
                ),
                structured_output_state=payload.structured_output_state,
            )

        self._client.error_handler.log_error(attempt, model, status_code, error_message, request_id)
        return AttemptOutcome(
            error_text=error_message,
            data=data,
            latency=latency,
            model_reported=model_reported,
            error_context=error_context,
            should_try_next_model=True,
            structured_output_state=payload.structured_output_state,
        )

    def _build_truncation_outcome(
        self,
        *,
        payload: AttemptRequestPayload,
        attempt: int,
        text: Any,
        max_tokens: int | None,
    ) -> AttemptOutcome:
        current_max = max_tokens or 8192
        truncation_recovery = TruncationRecovery(
            original_max_tokens=current_max,
            suggested_max_tokens=min(int(current_max * 1.5), 32768),
        )

        if payload.rf_included and payload.response_format_current:
            if payload.rf_mode_current == "json_schema":
                return AttemptOutcome(
                    retry=RetryDirective(
                        rf_mode="json_object",
                        response_format={"type": "json_object"},
                        backoff_needed=True,
                        truncation_recovery=truncation_recovery,
                    ),
                    structured_output_state=StructuredOutputState(used=True, mode="json_object"),
                )
            if payload.rf_mode_current == "json_object":
                return AttemptOutcome(
                    retry=RetryDirective(
                        rf_mode=None,
                        response_format=None,
                        backoff_needed=True,
                        truncation_recovery=truncation_recovery,
                    ),
                    structured_output_state=StructuredOutputState(),
                )

        if attempt < self._client.error_handler._max_retries:
            return AttemptOutcome(
                retry=RetryDirective(
                    rf_mode=payload.rf_mode_current,
                    response_format=payload.response_format_current,
                    backoff_needed=True,
                    truncation_recovery=truncation_recovery,
                ),
                structured_output_state=payload.structured_output_state,
            )
        return AttemptOutcome(
            error_text="completion_truncated",
            response_text=text if isinstance(text, str) else None,
            should_try_next_model=True,
            structured_output_state=payload.structured_output_state,
        )

    def _validate_structured_success_payload(
        self,
        *,
        text: Any,
        payload: AttemptRequestPayload,
        attempt: int,
        model: str,
    ) -> tuple[Any, AttemptOutcome | None]:
        if not (payload.rf_included and payload.response_format_current):
            return text, None

        is_valid, processed_text = self._client.response_processor.validate_structured_response(
            text,
            payload.rf_included,
            payload.response_format_current,
        )
        if is_valid:
            return processed_text, None

        if (
            payload.rf_mode_current == "json_schema"
            and attempt < self._client.error_handler._max_retries
        ):
            logger.warning(
                "structured_output_downgrading_json_schema_to_json_object",
                extra={"model": model, "attempt": attempt + 1},
            )
            logger.info(
                "structured_output_disabled_for_model",
                extra={
                    "model": model,
                    "reason": "auto_fallback_after_json_schema_parse_error",
                    "attempt": attempt + 1,
                },
            )
            return text, AttemptOutcome(
                retry=RetryDirective(
                    rf_mode="json_object",
                    response_format={"type": "json_object"},
                    backoff_needed=True,
                ),
                structured_output_state=StructuredOutputState(used=True, mode="json_object"),
            )

        if (
            payload.rf_mode_current == "json_object"
            and attempt < self._client.error_handler._max_retries
        ):
            logger.warning(
                "structured_output_disabling_after_json_object_failure",
                extra={"model": model, "attempt": attempt + 1},
            )
            logger.info(
                "structured_output_disabled_for_model",
                extra={
                    "model": model,
                    "reason": "auto_fallback_after_422",
                    "attempt": attempt + 1,
                },
            )
            return text, AttemptOutcome(
                retry=RetryDirective(
                    rf_mode=None,
                    response_format=None,
                    backoff_needed=True,
                ),
                structured_output_state=StructuredOutputState(),
            )

        return text, AttemptOutcome(
            error_text="structured_output_parse_error",
            response_text=processed_text or None,
            should_try_next_model=True,
            structured_output_state=StructuredOutputState(
                used=payload.structured_output_state.used,
                mode=payload.structured_output_state.mode,
                parse_error=True,
            ),
        )

    def _maybe_downgrade_on_response_format_error(
        self,
        *,
        status_code: int,
        data: dict[str, Any],
        payload: AttemptRequestPayload,
        attempt: int,
        model: str,
        request_id: int | None,
    ) -> AttemptOutcome | None:
        if not self._client.response_processor.should_downgrade_response_format(
            status_code,
            data,
            payload.rf_included,
        ):
            return None

        should_downgrade, new_mode = self._client.error_handler.should_downgrade_response_format(
            status_code,
            data,
            payload.rf_mode_current or "",
            payload.rf_included,
            attempt,
        )
        if not should_downgrade:
            return None
        if new_mode:
            self._client.error_handler.log_response_format_downgrade(
                model,
                "json_schema",
                new_mode,
                request_id,
            )
            return AttemptOutcome(
                retry=RetryDirective(
                    rf_mode=new_mode,
                    response_format={"type": "json_object"} if new_mode == "json_object" else None,
                    backoff_needed=True,
                ),
                structured_output_state=StructuredOutputState(
                    used=True,
                    mode=new_mode,
                ),
            )

        self._client.error_handler.log_structured_outputs_disabled(model, request_id)
        return AttemptOutcome(
            retry=RetryDirective(
                rf_mode=payload.rf_mode_current,
                response_format=None,
                backoff_needed=True,
            ),
            structured_output_state=StructuredOutputState(),
        )

    def _maybe_downgrade_on_endpoint_capability_error(
        self,
        *,
        status_code: int,
        error_message: str,
        error_context: dict[str, Any],
        payload: AttemptRequestPayload,
        model: str,
        request_id: int | None,
    ) -> AttemptOutcome | None:
        api_error_lower = str(error_context.get("api_error", "")).lower()
        if not (
            payload.rf_included
            and payload.response_format_current
            and (
                status_code == 404
                or "no endpoints found" in error_message.lower()
                or "no endpoints found" in api_error_lower
                or "does not support structured" in api_error_lower
            )
        ):
            return None

        if payload.rf_mode_current == "json_schema":
            self._client.error_handler.log_response_format_downgrade(
                model,
                "json_schema",
                "json_object",
                request_id,
            )
            return AttemptOutcome(
                retry=RetryDirective(
                    rf_mode="json_object",
                    response_format={"type": "json_object"},
                    backoff_needed=True,
                ),
                structured_output_state=StructuredOutputState(used=True, mode="json_object"),
            )

        if payload.rf_mode_current == "json_object":
            self._client.error_handler.log_structured_outputs_disabled(model, request_id)
            return AttemptOutcome(
                retry=RetryDirective(
                    rf_mode=payload.rf_mode_current,
                    response_format=None,
                    backoff_needed=True,
                ),
                structured_output_state=StructuredOutputState(),
            )
        return None

    def _extract_finish_reason(self, data: dict[str, Any]) -> tuple[Any, Any]:
        finish_reason = None
        native_finish_reason = None
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            first_choice = choices[0] or {}
            if isinstance(first_choice, dict):
                finish_reason = first_choice.get("finish_reason")
                native_finish_reason = first_choice.get("native_finish_reason")
        return finish_reason, native_finish_reason

    def _estimate_cost_if_missing(
        self,
        *,
        cost_usd: float | None,
        tokens_prompt: Any,
        tokens_completion: Any,
    ) -> float | None:
        if cost_usd is not None or tokens_prompt is None or tokens_completion is None:
            return cost_usd
        if self._client._price_input_per_1k is None or self._client._price_output_per_1k is None:
            return cost_usd
        try:
            return (float(tokens_prompt) / 1000.0) * self._client._price_input_per_1k + (
                float(tokens_completion) / 1000.0
            ) * self._client._price_output_per_1k
        except Exception:
            return None
