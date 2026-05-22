"""Anthropic request builder for constructing API payloads."""

from __future__ import annotations

from typing import Any

from app.adapters.llm.message_sanitizer import sanitize_messages_for_logging
from app.core.logging_utils import get_logger, redact_headers_for_logging

logger = get_logger(__name__)


# Anthropic pricing per 1M tokens (as of 2024)
# https://www.anthropic.com/pricing
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-5-20250929": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20240620": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229": {"input": 3.00, "output": 15.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
}

# Beta header for structured outputs
STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-11-13"


class AnthropicRequestBuilder:
    """Builds request headers and payloads for Anthropic API calls."""

    def __init__(
        self,
        api_key: str,
        *,
        enable_structured_outputs: bool = True,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        self._api_key = api_key
        self._enable_structured_outputs = enable_structured_outputs
        self._anthropic_version = anthropic_version

    def build_headers(self, use_structured_outputs: bool = False) -> dict[str, str]:
        """Build HTTP headers for the request.

        Args:
            use_structured_outputs: Whether structured outputs are being used.

        Returns:
            Headers dictionary.
        """
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
            "anthropic-version": self._anthropic_version,
        }

        # Add beta header for structured outputs
        if use_structured_outputs and self._enable_structured_outputs:
            headers["anthropic-beta"] = STRUCTURED_OUTPUTS_BETA

        return headers

    def build_request_body(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        top_p: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the request body for Anthropic messages API.

        Key difference: Anthropic uses a top-level `system` parameter instead of
        having system messages in the messages array.

        Args:
            model: Model name to use.
            messages: List of message dictionaries.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            top_p: Nucleus sampling parameter.
            response_format: Optional structured output format.

        Returns:
            Request body dictionary.
        """
        # Extract system message from messages array
        system_content, filtered_messages = self._extract_system_message(messages)

        # Convert messages to Anthropic format
        anthropic_messages = self._convert_messages(filtered_messages)

        body: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 4096,  # Anthropic requires max_tokens
        }

        # Add system prompt as top-level parameter (Anthropic-specific)
        if system_content:
            body["system"] = system_content

        # Only set temperature if it's reasonable (Anthropic max is 1.0)
        if temperature is not None:
            body["temperature"] = min(temperature, 1.0)

        if top_p is not None:
            body["top_p"] = top_p

        # Handle structured outputs (Anthropic uses output_format)
        if response_format and self._enable_structured_outputs:
            output_format = self._build_output_format(response_format)
            if output_format:
                body["output_format"] = output_format

        return body

    def _extract_system_message(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Extract system message and return remaining messages.

        Anthropic requires system prompt as a top-level parameter, not in messages.

        Args:
            messages: Original messages list.

        Returns:
            Tuple of (system_content, filtered_messages).
        """
        system_content: str | None = None
        filtered: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                # Concatenate multiple system messages if present
                system_content = f"{system_content}\n\n{content}" if system_content else content
            else:
                filtered.append(msg)

        return system_content, filtered

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert messages to Anthropic format.

        Anthropic uses a slightly different format with content as a list of blocks.

        Args:
            messages: Messages in standard format.

        Returns:
            Messages in Anthropic format.
        """
        anthropic_messages: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Map roles (Anthropic only supports "user" and "assistant")
            if role not in ("user", "assistant"):
                role = "user"  # Default unknown roles to user

            # Anthropic can accept content as string or list of blocks
            # We'll use string for simplicity
            anthropic_messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        return anthropic_messages

    def _build_output_format(self, response_format: dict[str, Any]) -> dict[str, Any] | None:
        """Build Anthropic-compatible output_format.

        Anthropic uses output_format instead of response_format, with different structure.

        Args:
            response_format: Input response format specification.

        Returns:
            Anthropic-compatible output format, or None if not applicable.
        """
        rf_type = response_format.get("type", "json_object")

        if rf_type == "json_object":
            # Basic JSON mode
            return {"type": "json"}

        if rf_type == "json_schema":
            json_schema = response_format.get("json_schema", {})
            schema = json_schema.get("schema", {})
            name = json_schema.get("name", "response")

            if not schema:
                return {"type": "json"}

            return {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "schema": schema,
                },
            }

        return None

    def get_redacted_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Return headers with sensitive values redacted."""
        return redact_headers_for_logging(headers)

    sanitize_messages = staticmethod(sanitize_messages_for_logging)


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Calculate the cost of an Anthropic API call.

    Args:
        model: Model name used.
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of output tokens.

    Returns:
        Estimated cost in USD, or None if model pricing is unknown.
    """
    pricing = ANTHROPIC_PRICING.get(model)
    if not pricing:
        # Try partial match
        for known_model, prices in ANTHROPIC_PRICING.items():
            if model.startswith(known_model.rsplit("-", 1)[0]):
                pricing = prices
                break

    if not pricing:
        return None

    # Pricing is per 1M tokens
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]

    return input_cost + output_cost
