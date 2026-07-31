"""OpenAI-compatible direct LLM client used by OpenAI and Ollama."""

from __future__ import annotations

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


class OpenAICompatibleLLMClient(BaseLLMClient):
    """Small adapter for OpenAI-compatible chat-completions APIs."""

    def __init__(
        self,
        *,
        provider_name: str,
        api_key: str | None,
        model: str,
        base_url: str,
        temperature: float,
        max_tokens: int | None,
        timeout_sec: int,
        max_retries: int,
        max_response_size_mb: int,
        circuit_breaker: CircuitBreaker | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
        price_input_per_1k: float | None = None,
        price_output_per_1k: float | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url.rstrip("/"),
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            max_response_size_mb=max_response_size_mb,
            circuit_breaker=circuit_breaker,
            audit=audit,
            price_input_per_1k=price_input_per_1k,
            price_output_per_1k=price_output_per_1k,
        )
        self._provider_name = provider_name
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._default_max_tokens = max_tokens

    @property
    def provider_name(self) -> str:
        return self._provider_name

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
        del stream, request_id, fallback_models_override, on_stream_delta
        del per_model_timeout_sec, per_model_timeout_overrides, budget_tight_ratio
        del truncation_max_count
        model = model_override or self._model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self._temperature,
        }
        token_limit = max_tokens if max_tokens is not None else self._default_max_tokens
        if token_limit is not None:
            payload["max_tokens"] = token_limit
        if top_p is not None:
            payload["top_p"] = top_p
        if response_format is not None:
            payload["response_format"] = response_format

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        started = time.perf_counter()
        async with self._request_context() as client:
            response = await client.post("/chat/completions", json=payload, headers=headers)
        latency_ms = int((time.perf_counter() - started) * 1000)
        data, error = self._parse_http_response(response, model, latency_ms, self.provider_name)
        if error is not None:
            return error

        # `.get("choices", [{}])` only substitutes the default when the key is
        # absent. Ollama and several proxies answer 200 with an explicitly empty
        # list, and indexing that raised IndexError outside _request_context --
        # escaping every caller of chat(), not just the retry loop.
        choices = (data or {}).get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        usage = (data or {}).get("usage") if isinstance(data, dict) else {}
        tokens_prompt = _usage_int(usage, "prompt_tokens")
        tokens_completion = _usage_int(usage, "completion_tokens")
        cost_usd = self._token_cost_usd(tokens_prompt, tokens_completion)
        if not isinstance(content, str) or not content.strip():
            return LLMCallResult(
                status=CallStatus.ERROR,
                model=model,
                response_text=None,
                response_json=data,
                error_text="Provider returned an empty chat completion",
                tokens_prompt=tokens_prompt,
                tokens_completion=tokens_completion,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                endpoint="/chat/completions",
            )
        return LLMCallResult(
            status=CallStatus.OK,
            model=model,
            response_text=content,
            response_json=data,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            endpoint="/chat/completions",
            structured_output_used=response_format is not None,
            structured_output_mode=_response_format_mode(response_format),
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
        async def _call() -> LLMCallResult:
            return await self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_id=request_id,
                model_override=model_override,
                fallback_models_override=fallback_models_override,
                response_format={"type": "json_object"},
            )

        return await self._run_structured_attempts(
            model=model_override or self._model,
            response_model=response_model,
            max_retries=max_retries,
            call=_call,
        )


def _usage_int(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _response_format_mode(response_format: dict[str, Any] | None) -> str | None:
    if not response_format:
        return None
    value = response_format.get("type")
    return str(value) if value else None
