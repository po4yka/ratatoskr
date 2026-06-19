"""Error handling and retry logic for OpenRouter API calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.adapter_models.llm.llm_models import LLMCallResult
from app.core.backoff import sleep_backoff as _sleep_backoff
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger
from app.utils.retry_utils import is_retryable_status_code

if TYPE_CHECKING:
    from collections.abc import Callable


class ErrorHandler:
    """Handles errors, retries, and fallback logic for OpenRouter API calls."""

    def __init__(
        self,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
        auto_fallback_structured: bool = True,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._audit = audit
        self._auto_fallback_structured = auto_fallback_structured
        self._logger = get_logger(__name__)

    async def sleep_backoff(self, attempt: int) -> None:
        """Sleep with exponential backoff and jitter."""
        await _sleep_backoff(attempt, self._backoff_base)

    def should_retry(self, status_code: int, attempt: int) -> bool:
        """Determine if a request should be retried based on status code and attempt."""
        if attempt >= self._max_retries:
            return False

        # Retryable errors (408, 429, 5xx)
        return is_retryable_status_code(status_code)

    def is_non_retryable_error(self, status_code: int) -> bool:
        """Check if error is non-retryable."""
        return status_code in (400, 401, 402, 403)

    def is_schema_construct_rejection(self, data: dict[str, Any]) -> bool:
        """Detect 400s caused by specific JSON Schema constructs.

        Differentiates "this provider does not accept ``additionalProperties``
        / ``oneOf`` / ``$ref``" from blanket "response_format not supported".
        The former is worth progressively simplifying via
        :mod:`app.adapters.openrouter.schema_simplifier` before falling
        through to the existing binary ``json_schema -> json_object -> off``
        downgrade.
        """
        if not isinstance(data, dict):
            return False
        import json as _json

        err_dump = _json.dumps(data).lower()
        construct_keywords = (
            "additionalproperties",
            "oneof",
            "anyof",
            "$ref",
            "$defs",
        )
        return any(keyword in err_dump for keyword in construct_keywords)

    def is_provider_specific_rejection(self, data: dict[str, Any]) -> bool:
        """Check if a 400 error is from an upstream provider, not OpenRouter.

        Provider-specific rejections (content policy, format incompatibility,
        resource limits) should trigger model fallback since other providers
        may accept the same request.

        OpenRouter wraps these with metadata.provider_name set to the upstream
        provider (e.g. "Azure", "Anthropic", "Alibaba").
        """
        err = data.get("error")
        if not isinstance(err, dict):
            return False
        metadata = err.get("metadata")
        if not isinstance(metadata, dict):
            return False
        return bool(metadata.get("provider_name"))

    def should_try_next_model(
        self,
        status_code: int,
        error_text: str | None = None,
    ) -> bool:
        """Determine if we should try the next model in fallback list.

        Triggers model fallback for:
        - 404: Model not found / no endpoints
        - 408: Request timeout (server-side)
        - 504: Gateway timeout (upstream provider slow)
        - Any error whose text contains "timeout" (case-insensitive)
        """
        if status_code in (404, 408, 504):
            return True
        if error_text and "timeout" in error_text.lower():
            return True
        return False

    _RETRY_AFTER_MAX_SEC: int = 120

    async def handle_rate_limit(self, response_headers: Any) -> None:
        """Handle rate limiting with proper delay.

        The Retry-After value is capped at _RETRY_AFTER_MAX_SEC to prevent
        an attacker-controlled header from causing an unbounded sleep.
        """
        retry_after = response_headers.get("retry-after")
        if retry_after:
            try:
                requested_seconds = int(retry_after)
                retry_seconds = min(requested_seconds, self._RETRY_AFTER_MAX_SEC)
                if requested_seconds > self._RETRY_AFTER_MAX_SEC:
                    self._logger.warning(
                        "retry_after_header_capped",
                        extra={
                            "requested_seconds": requested_seconds,
                            "capped_seconds": retry_seconds,
                        },
                    )
                await asyncio.sleep(retry_seconds)
            except (ValueError, TypeError) as e:
                self._logger.warning(
                    "invalid_retry_after_header",
                    extra={"retry_after": retry_after, "error": str(e)},
                )

    def should_downgrade_response_format(
        self,
        status_code: int,
        data: dict[str, Any],
        rf_mode_current: str,
        rf_included: bool,
        attempt: int,
    ) -> tuple[bool, str | None]:
        """Check if response format should be downgraded."""
        if not self._auto_fallback_structured:
            return False, None

        if status_code == 400 and rf_included:
            import json

            err_dump = json.dumps(data).lower()
            if "response_format" in err_dump or "output_config.format.schema" in err_dump:
                # Try downgrading from json_schema to json_object
                if rf_mode_current == "json_schema":
                    return True, "json_object"
                # If json_object also fails, disable structured outputs
                return True, None
        return False, None

    def build_error_result(
        self,
        model: str | None,
        text: str | None,
        data: dict[str, Any] | None,
        usage: dict[str, Any],
        latency: int,
        error_message: str,
        headers: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        error_context: dict[str, Any] | None = None,
        retry_exhausted: bool = False,
    ) -> LLMCallResult:
        """Build error result consistently."""
        return LLMCallResult(
            status=CallStatus.ERROR,
            model=model,
            response_text=text,
            response_json=data,
            openrouter_response_text=text,
            openrouter_response_json=data,
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            cost_usd=None,
            latency_ms=latency,
            retry_exhausted=retry_exhausted,
            error_text=error_message,
            request_headers=headers,
            request_messages=messages,
            endpoint="/api/v1/chat/completions",
            structured_output_used=False,
            structured_output_mode=None,
            error_context=error_context,
        )

    def _audit_event(self, level: str, event: str, details: dict[str, Any]) -> None:
        """Emit a single audit event if an audit function is configured."""
        if self._audit:
            self._audit(level, event, details)

    def log_attempt(self, attempt: int, model: str, request_id: int | None = None) -> None:
        """Log attempt information."""
        self._audit_event(
            "INFO",
            "openrouter_attempt",
            {"attempt": attempt, "model": model, "request_id": request_id},
        )

    def log_success(
        self,
        attempt: int,
        model: str,
        status_code: int,
        latency: int,
        structured_output_used: bool,
        structured_output_mode: str | None,
        request_id: int | None = None,
    ) -> None:
        """Log successful request."""
        self._audit_event(
            "INFO",
            "openrouter_success",
            {
                "attempt": attempt,
                "model": model,
                "status": status_code,
                "latency_ms": latency,
                "structured_output": structured_output_used,
                "rf_mode": structured_output_mode,
                "request_id": request_id,
            },
        )

    def log_error(
        self,
        attempt: int,
        model: str,
        status_code: int,
        error_message: str,
        request_id: int | None = None,
        severity: str = "ERROR",
    ) -> None:
        """Log error information."""
        self._audit_event(
            severity,
            "openrouter_error",
            {
                "attempt": attempt,
                "model": model,
                "status": status_code,
                "error": error_message,
                "request_id": request_id,
            },
        )

    def log_fallback(
        self,
        from_model: str,
        to_model: str,
        request_id: int | None = None,
    ) -> None:
        """Log model fallback."""
        self._audit_event(
            "WARN",
            "openrouter_fallback",
            {
                "from_model": from_model,
                "to_model": to_model,
                "request_id": request_id,
            },
        )

    def log_exhausted(
        self,
        models_tried: list[str],
        attempts_each: int,
        error: str | None,
        request_id: int | None = None,
    ) -> None:
        """Log when all models and retries are exhausted."""
        self._audit_event(
            "ERROR",
            "openrouter_exhausted",
            {
                "models_tried": models_tried,
                "attempts_each": attempts_each,
                "error": error,
                "request_id": request_id,
            },
        )

    def log_skip_model(self, model: str, reason: str, request_id: int | None = None) -> None:
        """Log when a model is skipped."""
        self._audit_event(
            "WARN",
            f"openrouter_skip_model_{reason}",
            {"model": model, "request_id": request_id},
        )

    def log_response_format_downgrade(
        self, model: str, from_mode: str, to_mode: str, request_id: int | None = None
    ) -> None:
        """Log response format downgrade."""
        self._audit_event(
            "WARN",
            "openrouter_downgrade_json_schema_to_object",
            {"model": model, "request_id": request_id},
        )

        self._logger.warning(
            "downgrade_response_format",
            extra={
                "model": model,
                "from": from_mode,
                "to": to_mode,
            },
        )

    def log_structured_outputs_disabled(self, model: str, request_id: int | None = None) -> None:
        """Log when structured outputs are disabled."""
        self._audit_event(
            "WARN",
            "openrouter_disable_structured_outputs",
            {"model": model, "request_id": request_id},
        )

        self._logger.warning(
            "disable_structured_outputs",
            extra={"model": model, "request_id": request_id},
        )
        self._logger.info(
            "structured_output_disabled_for_model",
            extra={
                "model": model,
                "reason": "explicit_disable",
                "request_id": request_id,
            },
        )

    def log_truncated_completion(
        self,
        model: str,
        finish_reason: str | None,
        native_finish_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Log when a completion stops because of output length limits."""
        self._audit_event(
            "WARN",
            "openrouter_truncated_completion",
            {
                "model": model,
                "finish_reason": finish_reason,
                "native_finish_reason": native_finish_reason,
                "request_id": request_id,
            },
        )

        self._logger.warning(
            "completion_truncated",
            extra={
                "model": model,
                "finish_reason": finish_reason,
                "native_finish_reason": native_finish_reason,
            },
        )
