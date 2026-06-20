"""Direct Anthropic Messages API client."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from app.adapter_models.llm.llm_models import LLMCallResult, StructuredLLMResult
from app.adapters.llm.base_client import BaseLLMClient
from app.core.call_status import CallStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.utils.circuit_breaker import CircuitBreaker

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class AnthropicDirectLLMClient(BaseLLMClient):
    """Small adapter for Anthropic's Messages API."""

    provider_name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        version: str,
        temperature: float,
        max_tokens: int,
        timeout_sec: int,
        max_retries: int,
        max_response_size_mb: int,
        circuit_breaker: CircuitBreaker | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url.rstrip("/"),
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            max_response_size_mb=max_response_size_mb,
            circuit_breaker=circuit_breaker,
            audit=audit,
        )
        self._api_key = api_key
        self._model = model
        self._version = version
        self._temperature = temperature
        self._default_max_tokens = max_tokens

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
        on_stream_delta: Callable[[str], Awaitable[None] | None] | None = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> LLMCallResult:
        del stream, request_id, response_format, fallback_models_override, on_stream_delta
        del per_model_timeout_sec, per_model_timeout_overrides, budget_tight_ratio
        del truncation_max_count
        model = model_override or self._model
        system, anthropic_messages = _split_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
        }
        if system:
            payload["system"] = system
        if top_p is not None:
            payload["top_p"] = top_p

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
        }
        started = time.perf_counter()
        async with self._request_context() as client:
            response = await client.post("/messages", json=payload, headers=headers)
        latency_ms = int((time.perf_counter() - started) * 1000)
        data, error = self._parse_http_response(response, model, latency_ms, self.provider_name)
        if error is not None:
            return error

        content = _extract_text_content(data)
        usage = (data or {}).get("usage") if isinstance(data, dict) else {}
        if not content.strip():
            return LLMCallResult(
                status=CallStatus.ERROR,
                model=model,
                response_text=None,
                response_json=data,
                error_text="Provider returned an empty message",
                tokens_prompt=_usage_int(usage, "input_tokens"),
                tokens_completion=_usage_int(usage, "output_tokens"),
                latency_ms=latency_ms,
                endpoint="/messages",
            )
        return LLMCallResult(
            status=CallStatus.OK,
            model=model,
            response_text=content,
            response_json=data,
            tokens_prompt=_usage_int(usage, "input_tokens"),
            tokens_completion=_usage_int(usage, "output_tokens"),
            latency_ms=latency_ms,
            endpoint="/messages",
        )

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[_ModelT],
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        request_id: int | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
    ) -> StructuredLLMResult[_ModelT]:
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            result = await self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_id=request_id,
                model_override=model_override,
                fallback_models_override=fallback_models_override,
            )
            if result.status != CallStatus.OK or result.response_text is None:
                http_status: int | None = None
                if isinstance(result.error_context, dict):
                    http_status = result.error_context.get("status_code")
                cause = RuntimeError(result.error_text or "Structured chat failed")
                last_error = cause
                # Non-retryable 4xx (anything except 429 Too Many Requests): break immediately.
                if http_status is not None and 400 <= http_status < 500 and http_status != 429:
                    break
                continue
            try:
                parsed = response_model.model_validate(json.loads(result.response_text))
            except Exception as exc:  # pragma: no cover - exercised through retry outcome
                last_error = exc
                continue
            return StructuredLLMResult(
                parsed=parsed,
                tokens_prompt=result.tokens_prompt,
                tokens_completion=result.tokens_completion,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                retry_count=attempt,
                model_used=result.model,
            )
        raise RuntimeError(f"Structured chat failed after retries: {last_error}") from last_error


def _split_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content", "")
        if role == "system":
            system_parts.append(str(content))
            continue
        converted.append(
            {"role": "assistant" if role == "assistant" else "user", "content": content}
        )
    return "\n\n".join(system_parts) or None, converted


def _extract_text_content(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _usage_int(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
