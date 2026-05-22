"""OpenAI request builder for constructing API payloads."""

from __future__ import annotations

from typing import Any

from app.adapters.llm.message_sanitizer import sanitize_messages_for_logging
from app.core.logging_utils import get_logger, redact_headers_for_logging

logger = get_logger(__name__)


# OpenAI pricing per 1M tokens (as of 2024)
# https://openai.com/api/pricing/
OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-08-06": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-05-13": {"input": 5.00, "output": 15.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4-turbo-2024-04-09": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-4-0613": {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    "gpt-3.5-turbo-0125": {"input": 0.50, "output": 1.50},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-2024-12-17": {"input": 15.00, "output": 60.00},
    "o1-preview": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
}


class OpenAIRequestBuilder:
    """Builds request headers and payloads for OpenAI API calls."""

    def __init__(
        self,
        api_key: str,
        *,
        organization: str | None = None,
        enable_structured_outputs: bool = True,
    ) -> None:
        self._api_key = api_key
        self._organization = organization
        self._enable_structured_outputs = enable_structured_outputs

    def build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the request."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._organization:
            headers["OpenAI-Organization"] = self._organization
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
        """Build the request body for OpenAI chat completions.

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
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }

        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        if top_p is not None:
            body["top_p"] = top_p

        # Handle structured outputs
        if response_format and self._enable_structured_outputs:
            body["response_format"] = self._build_response_format(response_format)

        return body

    def _build_response_format(self, response_format: dict[str, Any]) -> dict[str, Any]:
        """Build OpenAI-compatible response_format.

        OpenAI uses:
        - {"type": "json_object"} for basic JSON mode
        - {"type": "json_schema", "json_schema": {"strict": true, "schema": ...}} for structured

        Args:
            response_format: Input response format specification.

        Returns:
            OpenAI-compatible response format.
        """
        rf_type = response_format.get("type", "json_object")

        if rf_type == "json_object":
            return {"type": "json_object"}

        if rf_type == "json_schema":
            json_schema = response_format.get("json_schema", {})
            schema = json_schema.get("schema", {})
            name = json_schema.get("name", "response")

            return {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "strict": True,
                    "schema": schema,
                },
            }

        # Fallback to json_object
        return {"type": "json_object"}

    def get_redacted_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Return headers with sensitive values redacted."""
        return redact_headers_for_logging(headers)

    sanitize_messages = staticmethod(sanitize_messages_for_logging)


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Calculate the cost of an OpenAI API call.

    Args:
        model: Model name used.
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of output tokens.

    Returns:
        Estimated cost in USD, or None if model pricing is unknown.
    """
    pricing = OPENAI_PRICING.get(model)
    if not pricing:
        # Try without version suffix
        base_model = model.split("-", maxsplit=1)[0] if "-" in model else model
        for known_model, known_pricing in OPENAI_PRICING.items():
            if known_model.startswith(base_model):
                pricing = known_pricing
                break

    if not pricing:
        return None

    # Pricing is per 1M tokens
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]

    return input_cost + output_cost
