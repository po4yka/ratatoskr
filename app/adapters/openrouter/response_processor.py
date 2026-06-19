"""Response processor for OpenRouter API responses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.async_utils import raise_if_cancelled
from app.core.json_utils import extract_json
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class CacheMetrics:
    """Cache metrics extracted from OpenRouter response."""

    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_discount: float | None = None

    @property
    def cache_hit(self) -> bool:
        """Return True if cache was used (read tokens > 0)."""
        return self.cache_read_tokens > 0

    @property
    def total_cached_tokens(self) -> int:
        """Total tokens involved in caching (read + creation)."""
        return self.cache_read_tokens + self.cache_creation_tokens


class ResponseProcessor:
    """Processes and extracts content from OpenRouter API responses."""

    def __init__(self, enable_stats: bool = False) -> None:
        self._enable_stats = enable_stats

    def extract_structured_content(
        self, message_obj: dict[str, Any], rf_included: bool
    ) -> str | None:
        """Extract structured content from response message."""
        text = None

        # Prefer parsed field when structured outputs were requested
        if rf_included:
            parsed = message_obj.get("parsed")
            if parsed is not None:
                try:
                    text = json.dumps(parsed, ensure_ascii=False)
                except Exception:
                    text = str(parsed)

        # Fallback to content field
        if not text or (isinstance(text, str) and not text.strip()):
            content_field = message_obj.get("content")

            if isinstance(content_field, str):
                text = content_field
            elif isinstance(content_field, list):
                json_segments: list[str] = []
                text_segments: list[str] = []
                seen_json: set[str] = set()

                def append_json(value: Any) -> None:
                    json_str: str | None = None
                    if isinstance(value, dict | list):
                        try:
                            json_str = json.dumps(value, ensure_ascii=False)
                        except Exception:
                            return
                    elif isinstance(value, str):
                        stripped = value.strip()
                        if not stripped:
                            return
                        try:
                            parsed_value = json.loads(stripped)
                        except Exception:
                            return
                        if isinstance(parsed_value, dict | list):
                            json_str = json.dumps(parsed_value, ensure_ascii=False)
                        else:
                            return
                    else:
                        return

                    if json_str and json_str not in seen_json:
                        seen_json.add(json_str)
                        json_segments.append(json_str)

                def append_text(value: str) -> None:
                    stripped = value.strip()
                    if stripped:
                        text_segments.append(stripped)

                def maybe_append_text_or_json(value: str) -> None:
                    stripped = value.strip()
                    if not stripped:
                        return
                    try:
                        parsed_value = json.loads(stripped)
                    except Exception:
                        append_text(stripped)
                        return
                    if isinstance(parsed_value, dict | list):
                        append_json(parsed_value)
                    else:
                        append_text(stripped)

                def walk_content(part: Any, depth: int = 0) -> None:
                    if depth > 10:  # Safety limit for recursion
                        return

                    if isinstance(part, dict):
                        for key in ("json", "parsed", "arguments", "output"):
                            if key in part:
                                append_json(part[key])

                        function_block = part.get("function")
                        if isinstance(function_block, dict):
                            append_json(function_block.get("arguments"))

                        tool_calls = part.get("tool_calls")
                        if isinstance(tool_calls, list):
                            for call in tool_calls:
                                walk_content(call, depth + 1)

                        for key in ("text", "content", "reasoning"):
                            value = part.get(key)
                            if isinstance(value, str):
                                maybe_append_text_or_json(value)
                            elif isinstance(value, list | dict):
                                walk_content(value, depth + 1)

                        for key in ("data", "payload", "message"):
                            nested = part.get(key)
                            if isinstance(nested, dict | list):
                                append_json(nested)

                    elif isinstance(part, list):
                        for item in part:
                            walk_content(item, depth + 1)
                    elif isinstance(part, str):
                        append_text(part)

                try:
                    walk_content(content_field)
                    if json_segments:
                        text = "\n".join(json_segments)
                    elif text_segments:
                        text = "\n".join(text_segments)
                except Exception as exc:
                    raise_if_cancelled(exc)
                    logger.warning("content_walk_failed", extra={"error": str(exc)})

        # Try reasoning field for o1-style models
        if not text or (isinstance(text, str) and not text.strip()):
            reasoning = message_obj.get("reasoning")
            if reasoning and isinstance(reasoning, str):
                # Look for JSON in reasoning field
                start = reasoning.find("{")
                end = reasoning.rfind("}")
                if start != -1 and end != -1 and end > start:
                    try:
                        potential_json = reasoning[start : end + 1]
                        json.loads(potential_json)  # Validate JSON
                        text = potential_json
                    except Exception:
                        text = reasoning

        # Try function/tool calls
        if not text or (isinstance(text, str) and not text.strip()):
            tool_calls = message_obj.get("tool_calls") or []
            if tool_calls and isinstance(tool_calls, list):
                try:
                    first = tool_calls[0] or {}
                    fn = (first.get("function") or {}) if isinstance(first, dict) else {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        text = args
                    elif isinstance(args, dict):
                        text = json.dumps(args, ensure_ascii=False)
                except Exception as exc:
                    raise_if_cancelled(exc)
                    logger.warning("tool_call_extraction_failed", extra={"error": str(exc)})

        return text

    def extract_response_data(
        self, data: dict[str, Any], rf_included: bool
    ) -> tuple[str | None, dict[str, Any], float | None]:
        """Extract response text, usage data, and cost from API response.

        If OPENROUTER usage.total_cost is present, use it. Otherwise, return None for cost
        and let the caller optionally compute it using model-specific pricing.
        """
        text = None
        usage = data.get("usage") or {}
        cost_usd = None

        # Extract response content
        try:
            choices = data.get("choices") or []
            if choices:
                message_obj = choices[0].get("message", {}) or {}
                text = self.extract_structured_content(message_obj, rf_included)
        except Exception:
            text = None

        # Extract cost unconditionally — enable_stats only gates extra metrics logging,
        # not the basic cost capture that callers need for DB persistence and billing.
        try:
            # Usage cost may be directly in usage or inside choices
            raw = data.get("usage", {})
            if isinstance(raw, dict):
                # Direct check for OpenRouter's total_cost
                cost_val = raw.get("total_cost")
                if cost_val is not None:
                    cost_usd = float(cost_val)
                # Support other providers standard field names
                elif raw.get("cost") is not None:
                    cost_usd = float(raw["cost"])

            # Check for cost in choices (some local model gateways do this)
            if cost_usd is None and choices:
                cost_val = choices[0].get("cost")
                if cost_val is not None:
                    cost_usd = float(cost_val)
        except (TypeError, ValueError):
            cost_usd = None

        return text, usage, cost_usd

    def extract_cache_metrics(self, response: dict[str, Any]) -> CacheMetrics:
        """Extract cache metrics from OpenRouter response.

        OpenRouter includes cache metrics in the usage object:
        - cache_read_input_tokens: Tokens read from cache (cache hit)
        - cache_creation_input_tokens: Tokens added to cache (cache miss/write)
        - cache_discount: Cost discount from caching (if available)

        Args:
            response: Full API response dict

        Returns:
            CacheMetrics dataclass with extracted values
        """
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            return CacheMetrics()

        cache_read = 0
        cache_creation = 0
        cache_discount = None

        try:
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        except (TypeError, ValueError):
            cache_read = 0

        try:
            cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
        except (TypeError, ValueError):
            cache_creation = 0

        try:
            # Discount may be at top level or inside usage
            discount_val = response.get("cache_discount")
            if discount_val is None:
                discount_val = usage.get("cache_discount")

            if discount_val is not None:
                cache_discount = float(discount_val)
        except (TypeError, ValueError):
            cache_discount = None

        metrics = CacheMetrics(
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            cache_discount=cache_discount,
        )

        if self._enable_stats and (cache_read > 0 or cache_creation > 0):
            logger.info(
                "cache_metrics_extracted",
                extra={
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_creation,
                    "cache_discount": cache_discount,
                    "cache_hit": metrics.cache_hit,
                },
            )

        return metrics

    def validate_structured_response(
        self, text: str | None, rf_included: bool, requested_rf: dict[str, Any] | None
    ) -> tuple[bool, str | None]:
        """Validate structured output response and return (is_valid, processed_text)."""
        if not rf_included or not requested_rf:
            return True, text

        text_str = text or ""
        parsed = extract_json(text_str)

        if parsed is not None:
            try:
                processed_text = json.dumps(parsed, ensure_ascii=False)
            except Exception:
                processed_text = text_str

            # Reject an empty parsed value (empty dict/list) as a degenerate
            # structured response.  Domain-field contract validation is the
            # responsibility of the summarize-graph validate node, not this
            # generic transport layer.
            if not parsed:
                return False, processed_text

            return True, processed_text
        # Invalid JSON with structured outputs
        return False, text_str

    def inspect_completion_truncation(
        self, data: dict[str, Any]
    ) -> tuple[bool, str | None, str | None]:
        """Inspect response metadata and determine if the completion was truncated."""
        try:
            choices = data.get("choices") or []
            if not choices:
                return False, None, None

            first = choices[0] or {}
            finish_reason = first.get("finish_reason")
            native_finish_reason = first.get("native_finish_reason")

            finish_reason_str = finish_reason if isinstance(finish_reason, str) else None
            native_reason_str = (
                native_finish_reason if isinstance(native_finish_reason, str) else None
            )

            truncated = False
            if finish_reason_str:
                truncated = finish_reason_str.lower() in {"length", "max_tokens"}

            if native_reason_str and not truncated:
                normalized_native = native_reason_str.replace("-", "_").lower()
                if any(term in normalized_native for term in ("max_token", "length")):
                    truncated = True

            return truncated, finish_reason_str, native_reason_str
        except Exception:
            return False, None, None

    def should_downgrade_response_format(
        self, status_code: int, data: dict[str, Any], rf_included: bool
    ) -> bool:
        """Check if response format should be downgraded due to errors."""
        if status_code == 400 and rf_included:
            err_dump = json.dumps(data).lower()
            return "response_format" in err_dump
        return False

    def get_error_context(self, status_code: int, data: dict[str, Any]) -> dict[str, Any]:
        """Return structured error context for logging and user messaging."""
        error_messages = {
            400: "Invalid or missing request parameters",
            401: "Authentication failed (invalid or expired API key)",
            402: "Insufficient account balance",
            403: "Access forbidden (API key limit exceeded or invalid permissions)",
            404: "Requested resource not found",
            429: "Rate limit exceeded",
            500: "Internal server error",
        }

        base_message = error_messages.get(status_code, f"HTTP {status_code} error")
        api_error = None
        if isinstance(data, dict):
            raw_error = data.get("error")
            if isinstance(raw_error, dict):
                api_error = raw_error.get("message") or raw_error.get("code")
            elif isinstance(raw_error, str):
                api_error = raw_error

        # Enhance error message for specific OpenRouter API errors
        if status_code == 403 and api_error:
            api_error_lower = str(api_error).lower()
            if "key limit exceeded" in api_error_lower:
                base_message = "API key usage limit exceeded. Please check your OpenRouter account limits or upgrade your plan."
            elif (
                "manage it using" in api_error_lower
                and "openrouter.ai/settings/keys" in api_error_lower
            ):
                base_message = "API key limit exceeded. Please manage your key limits at https://openrouter.ai/settings/keys"

        provider = None
        if isinstance(data, dict):
            provider = data.get("provider")
        provider_detail = None
        if isinstance(provider, dict):
            provider_detail = provider.get("name") or provider.get("id")
        elif isinstance(provider, str):
            provider_detail = provider

        context = {
            "status_code": status_code,
            "message": base_message,
            "api_error": api_error,
        }
        if provider_detail:
            context["provider"] = provider_detail
        return context
