"""Data models for LLM interactions backed by Pydantic validation."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.core.call_status import CallStatus  # noqa: TC001


class LLMCallResult(BaseModel):
    """Result of an LLM API call with comprehensive metadata."""

    model_config = ConfigDict(extra="forbid")

    status: CallStatus = Field(description="High-level result status.")
    model: str | None = Field(default=None, description="Model that produced the response.")
    response_text: str | None = Field(
        default=None, description="Primary text response returned by the provider."
    )
    response_json: dict[str, Any] | None = Field(
        default=None, description="Structured JSON payload returned by the provider."
    )
    openrouter_response_text: str | None = Field(
        default=None, description="Raw OpenRouter response text (pre-parsing)."
    )
    openrouter_response_json: dict[str, Any] | None = Field(
        default=None, description="Raw OpenRouter JSON payload (pre-processing)."
    )
    tokens_prompt: int | None = Field(
        default=None, description="Prompt tokens consumed by the request."
    )
    tokens_completion: int | None = Field(
        default=None, description="Completion tokens produced by the request."
    )
    cost_usd: float | None = Field(default=None, description="Estimated USD cost for the request.")
    latency_ms: int | None = Field(
        default=None,
        description="Observed latency for the LLM request in milliseconds.",
    )
    fallback_model_used: str | None = Field(
        default=None,
        description="Fallback model that produced the terminal successful response.",
    )
    retry_exhausted: bool = Field(
        default=False,
        description="Whether the retry/fallback budget ended in a terminal failure.",
    )
    total_latency_ms: int | None = Field(
        default=None,
        description="Cascade-wide wall-clock latency from the first attempt.",
    )
    error_text: str | None = Field(default=None, description="Error message when the call fails.")
    request_headers: dict[str, Any] | None = Field(
        default=None, description="HTTP headers sent with the request."
    )
    request_messages: list[dict[str, Any]] | None = Field(
        default=None, description="Messages payload submitted to the chat endpoint."
    )
    endpoint: str | None = Field(
        default="/api/v1/chat/completions",
        description="Endpoint used for the LLM call.",
    )
    structured_output_used: bool = Field(
        default=False, description="Whether structured outputs were requested."
    )
    structured_output_mode: str | None = Field(
        default=None, description="Structured output mode requested (if any)."
    )
    error_context: dict[str, Any] | None = Field(
        default=None, description="Additional context about encountered errors."
    )
    cache_read_tokens: int | None = Field(
        default=None, description="Tokens read from cache (cache hit)."
    )
    cache_creation_tokens: int | None = Field(
        default=None, description="Tokens added to cache (cache write)."
    )
    cache_discount: float | None = Field(
        default=None, description="Cost discount from prompt caching."
    )
    models_attempted: list[tuple[str, str]] = Field(
        default_factory=list,
        description=(
            "Ordered list of (model_name, outcome) pairs from the fallback ladder. "
            "Outcomes: 'success', 'timeout', 'error', 'skipped_unsupported_structured', "
            "'non_content_response'."
        ),
    )
    per_model_attempts: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Ordered cascade of per-model attempts that did NOT produce the terminal "
            "result (timeouts, skipped models, and errors that triggered fallback). "
            "Each entry: {model, status, latency_ms, error_text, error_context, "
            "per_model_timeout_sec}. The terminal attempt is conveyed by the top-level "
            "fields and is not duplicated here."
        ),
    )


class ChatRequest(BaseModel):
    """Request parameters for chat completions."""

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]] = Field(
        description="Conversation messages to send to the chat endpoint."
    )
    temperature: float = Field(
        default=0.2, description="Sampling temperature for the chat completion."
    )
    max_tokens: int | None = Field(
        default=None, description="Maximum tokens to generate in the completion."
    )
    top_p: float | None = Field(default=None, description="Nucleus sampling parameter.")
    stream: StrictBool = Field(
        default=False,
        description="Whether to request a streaming response.",
    )
    request_id: int | None = Field(
        default=None, description="Internal request identifier for tracing."
    )
    response_format: dict[str, Any] | None = Field(
        default=None, description="Structured output schema requested from the model."
    )
    model_override: str | None = Field(
        default=None, description="Override model name for this specific call."
    )


_T = TypeVar("_T")


class StructuredLLMResult(BaseModel, Generic[_T]):
    """Result of a Pydantic-validated structured LLM call (via Instructor)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    parsed: _T
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    retry_count: int = 0
    model_used: str | None = None


__all__ = ["ChatRequest", "LLMCallResult", "StructuredLLMResult"]
