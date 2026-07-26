"""Request builder for OpenRouter API calls."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.adapters.openrouter.exceptions import ValidationError
from app.adapters.openrouter.model_capabilities import (
    get_caching_info,
    supports_automatic_caching,
    supports_explicit_caching,
)
from app.core.logging_utils import get_logger, redact_headers_for_logging

if TYPE_CHECKING:
    from app.adapter_models.llm.llm_models import ChatRequest

logger = get_logger(__name__)

# Injection-signal phrases stripped from user-role message text by
# RequestBuilder.sanitize_messages. Compiled once at import rather than rebuilt
# and recompiled per message on every chat() call. Cosmetic filter only, not a
# security boundary -- see sanitize_messages for the rationale.
_INJECTION_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(phrase, re.IGNORECASE)
    for phrase in (
        r"ignore previous instructions",
        r"forget previous instructions",
        r"system:",
        r"assistant:",
        r"user:",
    )
)


class RequestBuilder:
    """Builds and validates HTTP requests for OpenRouter API."""

    def __init__(
        self,
        api_key: str,
        http_referer: str | None = None,
        x_title: str | None = None,
        provider_order: list[str] | tuple[str, ...] | None = None,
        enable_structured_outputs: bool = True,
        structured_output_mode: str = "json_schema",
        require_parameters: bool = True,
        # Prompt caching settings
        enable_prompt_caching: bool = True,
        prompt_cache_ttl: str = "ephemeral",
        prompt_cache_ttl_anthropic: str = "1h",
        cache_system_prompt: bool = True,
        cache_large_content_threshold: int = 4096,
    ) -> None:
        self._api_key = api_key
        self._http_referer = http_referer
        self._x_title = x_title

        self._provider_order = list(provider_order or [])
        self._enable_structured_outputs = enable_structured_outputs
        self._structured_output_mode = structured_output_mode
        self._require_parameters = require_parameters
        # Prompt caching settings
        self._enable_prompt_caching = enable_prompt_caching
        self._prompt_cache_ttl = prompt_cache_ttl
        self._prompt_cache_ttl_anthropic = prompt_cache_ttl_anthropic
        self._cache_system_prompt = cache_system_prompt
        self._cache_large_content_threshold = cache_large_content_threshold

    def set_api_key(self, api_key: str) -> None:
        """Replace the bearer credential used for subsequent requests.

        The key is frozen here at construction but the Authorization header is
        built per request, so swapping it takes effect on the very next call
        without rebuilding the client or dropping pooled connections.
        """
        self._api_key = api_key

    def validate_chat_request(self, request: ChatRequest) -> None:
        """Validate chat request parameters."""
        self._validate_messages(request.messages)
        self._validate_temperature(request.temperature)
        self._validate_max_tokens(request.max_tokens)
        self._validate_top_p(request.top_p)
        self._validate_stream(request.stream)
        self._validate_request_id(request.request_id)

    def _validate_messages(self, messages: Any) -> None:
        if not messages or not isinstance(messages, list):
            msg = "Messages list is required"
            raise ValidationError(
                msg,
                context={"messages_count": len(messages) if messages else 0},
            )
        if len(messages) > 50:
            msg = f"Too many messages (max 50, got {len(messages)})"
            raise ValidationError(msg, context={"messages_count": len(messages)})

        for i, message in enumerate(messages):
            self._validate_message(i, message)

    def _validate_message(self, index: int, message: Any) -> None:
        if not isinstance(message, dict):
            error_msg = f"Message {index} must be a dictionary, got {type(message).__name__}"
            raise ValidationError(
                error_msg,
                context={"message_index": index, "message_type": type(message).__name__},
            )
        if "role" not in message or "content" not in message:
            error_msg = f"Message {index} missing required fields 'role' or 'content'"
            raise ValidationError(
                error_msg,
                context={
                    "message_index": index,
                    "missing_fields": [k for k in ["role", "content"] if k not in message],
                },
            )
        if not isinstance(message["role"], str) or message["role"] not in {
            "system",
            "user",
            "assistant",
        }:
            error_msg = (
                f"Message {index} has invalid role '{message.get('role', 'missing')}', "
                "must be one of: system, user, assistant"
            )
            raise ValidationError(
                error_msg,
                context={
                    "message_index": index,
                    "invalid_role": message.get("role"),
                    "valid_roles": ["system", "user", "assistant"],
                },
            )
        self._validate_message_content(index, message["content"])

    def _validate_message_content(self, message_index: int, content: Any) -> None:
        if isinstance(content, str):
            return
        if isinstance(content, list):
            for part_idx, part in enumerate(content):
                if not isinstance(part, dict):
                    error_msg = f"Message {message_index} content part {part_idx} must be a dict"
                    raise ValidationError(
                        error_msg,
                        context={"message_index": message_index, "part_index": part_idx},
                    )
                if part.get("type") == "text" and not isinstance(part.get("text"), str):
                    error_msg = (
                        f"Message {message_index} content part {part_idx} text must be a string"
                    )
                    raise ValidationError(
                        error_msg,
                        context={"message_index": message_index, "part_index": part_idx},
                    )
            return

        error_msg = (
            f"Message {message_index} content must be string or list, got {type(content).__name__}"
        )
        raise ValidationError(
            error_msg,
            context={"message_index": message_index, "content_type": type(content).__name__},
        )

    def _validate_temperature(self, temperature: Any) -> None:
        if not isinstance(temperature, int | float):
            msg = f"Temperature must be numeric, got {type(temperature).__name__}"
            raise ValidationError(
                msg,
                context={
                    "parameter": "temperature",
                    "value": temperature,
                    "type": type(temperature).__name__,
                },
            )
        if temperature < 0 or temperature > 2:
            msg = f"Temperature must be between 0 and 2, got {temperature}"
            raise ValidationError(msg, context={"parameter": "temperature", "value": temperature})

    def _validate_max_tokens(self, max_tokens: Any) -> None:
        if max_tokens is None:
            return
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            msg = f"Max tokens must be a positive integer, got {max_tokens}"
            raise ValidationError(
                msg,
                context={
                    "parameter": "max_tokens",
                    "value": max_tokens,
                    "type": type(max_tokens).__name__,
                },
            )
        if max_tokens > 100000:
            msg = f"Max tokens too large (max 100000, got {max_tokens})"
            raise ValidationError(msg, context={"parameter": "max_tokens", "value": max_tokens})

    def _validate_top_p(self, top_p: Any) -> None:
        if top_p is None:
            return
        if not isinstance(top_p, int | float):
            msg = f"Top_p must be numeric, got {type(top_p).__name__}"
            raise ValidationError(
                msg,
                context={
                    "parameter": "top_p",
                    "value": top_p,
                    "type": type(top_p).__name__,
                },
            )
        if top_p < 0 or top_p > 1:
            msg = f"Top_p must be between 0 and 1, got {top_p}"
            raise ValidationError(msg, context={"parameter": "top_p", "value": top_p})

    def _validate_stream(self, stream: Any) -> None:
        if isinstance(stream, bool):
            return
        msg = f"Stream must be boolean, got {type(stream).__name__}"
        raise ValidationError(
            msg,
            context={"parameter": "stream", "value": stream, "type": type(stream).__name__},
        )

    def _validate_request_id(self, request_id: Any) -> None:
        if request_id is None:
            return
        if isinstance(request_id, int) and request_id > 0:
            return
        msg = f"Invalid request_id (must be positive integer, got {request_id})"
        raise ValidationError(
            msg,
            context={
                "parameter": "request_id",
                "value": request_id,
                "type": type(request_id).__name__,
            },
        )

    def sanitize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Best-effort cosmetic filter applied only to user-role messages.

        Strips a small set of literal injection-signal phrases from user message
        text. This is NOT a security boundary — system and assistant messages are
        left untouched, and a determined adversary can bypass simple regex
        replacement. The function is intentionally narrow: it removes phrases that
        are almost never legitimate in user input and that reliably indicate a
        role-override attempt. Triple-backtick code fences are NOT stripped because
        they appear in valid user content (code pastes) and removing them silently
        corrupts that content without meaningfully blocking injection.
        """
        sanitized_messages = []
        for msg in messages:
            if msg["role"] == "user":
                content = msg["content"]
                if isinstance(content, str):
                    sanitized_content = content
                    for pat in _INJECTION_SIGNAL_PATTERNS:
                        sanitized_content = pat.sub("", sanitized_content)
                    if sanitized_content != content:
                        msg = {**msg, "content": sanitized_content}
                elif isinstance(content, list):
                    sanitized_parts = []
                    for part in content:
                        if part.get("type") == "text" and isinstance(part.get("text"), str):
                            sanitized_text = part["text"]
                            for pat in _INJECTION_SIGNAL_PATTERNS:
                                sanitized_text = pat.sub("", sanitized_text)
                            sanitized_parts.append({**part, "text": sanitized_text})
                        else:
                            sanitized_parts.append(part)
                    msg = {**msg, "content": sanitized_parts}
            sanitized_messages.append(msg)
        return sanitized_messages

    def build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the request."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": (self._http_referer or "https://github.com/your-repo"),
            "X-Title": self._x_title or "Ratatoskr Bot",
        }

    def build_request_body(
        self,
        model: str,
        messages: list[dict[str, Any]],
        request: ChatRequest,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the request body for the API call."""
        body = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
        }

        # Add optional parameters
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stream:
            body["stream"] = request.stream

        # Provider routing configuration
        provider_prefs: dict[str, Any] = {}
        if self._provider_order:
            provider_prefs["order"] = list(self._provider_order)

        # Add response format if structured outputs enabled
        if response_format and self._enable_structured_outputs:
            built_rf = self._build_response_format(response_format, self._structured_output_mode)
            if built_rf:
                body["response_format"] = built_rf

        # Attach provider preferences
        if provider_prefs:
            body["provider"] = provider_prefs

        return body

    def _build_response_format(
        self, response_format: dict[str, Any] | None, mode: str
    ) -> dict[str, Any] | None:
        """Build response format based on mode and input.

        Rules:
        - If caller passes a fully wrapped object (has "type"), pass through.
        - If caller passes a raw JSON Schema, wrap into OpenRouter shape when
          mode == json_schema.
        - If mode == json_object, request a generic JSON object.
        """
        if not response_format or not self._enable_structured_outputs:
            return None

        # Pass-through for already-wrapped response_format
        rf_type = response_format.get("type") if isinstance(response_format, dict) else None
        if isinstance(rf_type, str) and rf_type in {
            "json_schema",
            "json_object",
        }:
            return response_format

        # Caller provided a raw schema or helper dict; wrap appropriately
        if mode == "json_schema":
            # Accept either {schema: {...}, name?, strict?} or a plain
            # JSON Schema
            json_schema_block = (
                response_format.get("schema") if isinstance(response_format, dict) else None
            )
            schema_obj = (
                json_schema_block if isinstance(json_schema_block, dict) else response_format
            )
            name_val = response_format.get("name") if isinstance(response_format, dict) else None
            strict_val = (
                response_format.get("strict") if isinstance(response_format, dict) else None
            )

            return {
                "type": "json_schema",
                "json_schema": {
                    "name": name_val or "schema",
                    "strict": (True if strict_val is None else bool(strict_val)),
                    "schema": (schema_obj if isinstance(schema_obj, dict) else {}),
                },
            }

        # Fallback to basic JSON object request
        return {"type": "json_object"}

    def should_apply_compression(
        self, messages: list[dict[str, Any]], model: str
    ) -> tuple[bool, str | None]:
        """Determine if content compression should be applied."""
        total_content_length = sum(len(msg.get("content", "")) for msg in messages)
        model_lower = model.lower()

        compression_threshold = 1200000 if "gemini-3.1" in model_lower else 200000

        if total_content_length > compression_threshold:
            return True, "middle-out"
        return False, None

    def get_redacted_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Get headers with sensitive information redacted."""
        return redact_headers_for_logging(headers)

    def build_cacheable_messages(
        self,
        messages: list[dict[str, Any]],
        model: str,
        enable_caching: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Convert messages to cacheable format for supported providers.

        For Anthropic and Google providers, adds cache_control breakpoints to
        system messages and large content blocks to enable prompt caching.

        For providers with automatic caching (OpenAI, DeepSeek, Qwen, Moonshot),
        no modification is needed - caching happens server-side.

        Args:
            messages: List of message dicts with role and content
            model: Model identifier for provider detection
            enable_caching: Override for enabling caching (defaults to instance setting)

        Returns:
            Messages with cache_control added where appropriate
        """
        should_cache = enable_caching if enable_caching is not None else self._enable_prompt_caching
        caching_info = get_caching_info(model)
        provider = caching_info["provider"]

        # Log caching info on first request
        if caching_info["supports_caching"]:
            if supports_automatic_caching(model):
                logger.debug(
                    "prompt_caching_automatic",
                    extra={
                        "model": model,
                        "provider": provider,
                        "caching_type": "automatic",
                        "notes": caching_info["notes"],
                    },
                )
                # Automatic caching providers don't need message modification
                return messages

        # Only Anthropic and Google require explicit cache_control
        if not should_cache or not supports_explicit_caching(model):
            if not caching_info["supports_caching"]:
                logger.debug(
                    "prompt_caching_not_supported",
                    extra={
                        "model": model,
                        "provider": provider,
                        "reason": "Provider does not support prompt caching",
                    },
                )
            return messages

        result: list[dict[str, Any]] = []
        breakpoints_added = 0
        max_breakpoints = 4 if provider == "anthropic" else 1  # Gemini only uses last breakpoint
        # Anthropic supports a 1h TTL (2x write, 0.10x read) that amortizes well
        # across batched requests; Google's cache_control through OpenRouter does
        # not document the same surface, so keep the generic TTL there.
        effective_ttl = (
            self._prompt_cache_ttl_anthropic if provider == "anthropic" else self._prompt_cache_ttl
        )

        for i, msg in enumerate(messages):
            if self._should_cache_message(msg, provider, i, len(messages)):
                # Only add breakpoints up to the limit
                if breakpoints_added < max_breakpoints:
                    result.append(self._add_cache_control(msg, effective_ttl))
                    breakpoints_added += 1
                    logger.debug(
                        "cache_control_added",
                        extra={
                            "message_index": i,
                            "role": msg.get("role"),
                            "provider": provider,
                            "breakpoints": breakpoints_added,
                        },
                    )
                else:
                    result.append(msg)
            else:
                result.append(msg)

        if breakpoints_added > 0:
            logger.info(
                "prompt_caching_enabled",
                extra={
                    "provider": provider,
                    "model": model,
                    "breakpoints_added": breakpoints_added,
                },
            )

        return result

    def _should_cache_message(
        self,
        msg: dict[str, Any],
        provider: str,
        index: int,
        total_messages: int,
    ) -> bool:
        """Determine if a message should be cached.

        Args:
            msg: Message dict
            provider: Provider name
            index: Message index in list
            total_messages: Total number of messages

        Returns:
            True if message should have cache_control added
        """
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Always cache system messages if enabled
        if role == "system" and self._cache_system_prompt:
            return True

        # Cache large content blocks
        content_length = len(content) if isinstance(content, str) else 0
        # Rough estimate: ~4 chars per token
        estimated_tokens = content_length // 4

        # For Gemini, must meet minimum token threshold (4096)
        if provider == "google" and estimated_tokens < self._cache_large_content_threshold:
            return False

        return estimated_tokens >= self._cache_large_content_threshold

    def _add_cache_control(self, msg: dict[str, Any], ttl: str) -> dict[str, Any]:
        """Convert message to multipart format with cache_control.

        Args:
            msg: Original message dict
            ttl: Cache TTL string ('ephemeral' or '1h'); resolved per-provider
                 by the caller.

        Returns:
            Message with cache_control added to content
        """
        content = msg.get("content", "")

        # If content is already a list, add cache_control to last text part
        if isinstance(content, list):
            new_content = []
            for i, part in enumerate(content):
                if i == len(content) - 1 and isinstance(part, dict) and part.get("type") == "text":
                    # Add cache_control to last text part
                    new_part = dict(part)
                    new_part["cache_control"] = {"type": ttl}
                    new_content.append(new_part)
                else:
                    new_content.append(part)
            return {**msg, "content": new_content}

        # Convert string content to multipart format with cache_control
        if isinstance(content, str):
            return {
                **msg,
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": ttl},
                    }
                ],
            }

        # Return unchanged if content is neither string nor list
        return msg

    def estimate_content_tokens(self, content: str | list[Any]) -> int:
        """Estimate token count for content.

        Args:
            content: String or list of content parts

        Returns:
            Estimated token count (rough: ~4 chars per token)
        """
        if isinstance(content, str):
            return len(content) // 4
        if isinstance(content, list):
            total = 0
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"]) // 4
            return total
        return 0
