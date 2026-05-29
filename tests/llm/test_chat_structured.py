"""Tests for the chat_structured protocol method across LLM providers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.adapters.llm.anthropic.client import AnthropicClient
from app.adapters.llm.openai.client import OpenAIClient
from app.adapters.llm.protocol import LLMClientProtocol
from app.adapters.openrouter.openrouter_client import OpenRouterClient, OpenRouterClientConfig

# ---------------------------------------------------------------------------
# Minimal test schema
# ---------------------------------------------------------------------------


class _Fact(BaseModel):
    label: str
    value: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MESSAGES = [{"role": "user", "content": "Give me a fact."}]
_API_KEY = "test_api_key_12345"  # min 10 chars to pass validation


def _mock_completion(prompt_tokens: int = 100, completion_tokens: int = 40) -> MagicMock:
    """Build a fake openai.ChatCompletion with usage stats."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    comp = MagicMock()
    comp.usage = usage
    return comp


def _openrouter_client() -> OpenRouterClient:
    return OpenRouterClient(
        _API_KEY,
        OpenRouterClientConfig(max_retries=1),
        model="test-model",
    )


def _openai_client() -> OpenAIClient:
    return OpenAIClient(_API_KEY, model="gpt-4o")


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_protocol_has_chat_structured() -> None:
    assert hasattr(LLMClientProtocol, "chat_structured")


def test_openrouter_satisfies_protocol() -> None:
    client = _openrouter_client()
    assert hasattr(client, "chat_structured")
    assert callable(client.chat_structured)


def test_openai_satisfies_protocol() -> None:
    client = _openai_client()
    assert hasattr(client, "chat_structured")
    assert callable(client.chat_structured)


def test_anthropic_satisfies_protocol() -> None:
    client = AnthropicClient(_API_KEY)
    assert hasattr(client, "chat_structured")
    assert callable(client.chat_structured)


# ---------------------------------------------------------------------------
# OpenRouter: success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_chat_structured_success() -> None:
    client = _openrouter_client()
    expected = _Fact(label="speed of light", value="299792458 m/s")
    comp = _mock_completion(prompt_tokens=80, completion_tokens=20)

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(
        return_value=(expected, comp)
    )
    client._instructor_async_client = mock_instructor

    result = await client.chat_structured(_MESSAGES, response_model=_Fact, request_id=42)

    assert isinstance(result, StructuredLLMResult)
    assert result.parsed is expected
    assert result.tokens_prompt == 80
    assert result.tokens_completion == 20
    assert result.model_used == "test-model"
    mock_instructor.chat.completions.create_with_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_openrouter_chat_structured_records_cost_from_token_prices() -> None:
    """Structured calls estimate cost_usd from token counts x configured prices."""
    client = _openrouter_client()
    client._price_input_per_1k = 0.5
    client._price_output_per_1k = 1.5
    comp = _mock_completion(prompt_tokens=1000, completion_tokens=200)

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(
        return_value=(_Fact(label="x", value="y"), comp)
    )
    client._instructor_async_client = mock_instructor

    result = await client.chat_structured(_MESSAGES, response_model=_Fact)

    # 1000/1000 * 0.5 + 200/1000 * 1.5 = 0.5 + 0.3
    assert result.cost_usd == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_openrouter_chat_structured_prefers_provider_cost() -> None:
    """A provider-reported cost on usage wins over the token x price estimate."""
    client = _openrouter_client()
    client._price_input_per_1k = 0.5
    client._price_output_per_1k = 1.5
    comp = _mock_completion(prompt_tokens=1000, completion_tokens=200)
    comp.usage.cost = 0.123  # provider-reported cost

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(
        return_value=(_Fact(label="x", value="y"), comp)
    )
    client._instructor_async_client = mock_instructor

    result = await client.chat_structured(_MESSAGES, response_model=_Fact)

    assert result.cost_usd == pytest.approx(0.123)


# ---------------------------------------------------------------------------
# OpenRouter: model fallback on first-model failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_chat_structured_falls_back_to_second_model() -> None:
    client = OpenRouterClient(
        _API_KEY,
        OpenRouterClientConfig(max_retries=1),
        model="primary-model",
        fallback_models=["fallback-model"],
    )
    expected = _Fact(label="pi", value="3.14159")
    comp = _mock_completion()

    call_count = 0

    async def side_effect(**kwargs: object) -> tuple[_Fact, MagicMock]:
        nonlocal call_count
        call_count += 1
        if kwargs.get("model") == "primary-model":
            raise RuntimeError("primary model unavailable")
        return (expected, comp)

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(side_effect=side_effect)
    client._instructor_async_client = mock_instructor

    result = await client.chat_structured(_MESSAGES, response_model=_Fact)

    assert result.parsed is expected
    assert result.model_used == "fallback-model"
    assert call_count == 2


# ---------------------------------------------------------------------------
# OpenRouter: all models exhausted → raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_chat_structured_all_models_fail() -> None:
    client = _openrouter_client()

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    client._instructor_async_client = mock_instructor

    with pytest.raises(RuntimeError, match="boom"):
        await client.chat_structured(_MESSAGES, response_model=_Fact)


# ---------------------------------------------------------------------------
# OpenRouter: closed client raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_chat_structured_raises_when_closed() -> None:
    client = _openrouter_client()
    client._closed = True

    with pytest.raises(RuntimeError, match="closed"):
        await client.chat_structured(_MESSAGES, response_model=_Fact)


# ---------------------------------------------------------------------------
# OpenRouter: empty messages raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_chat_structured_raises_on_empty_messages() -> None:
    client = _openrouter_client()

    with pytest.raises(ValueError, match="empty"):
        await client.chat_structured([], response_model=_Fact)


# ---------------------------------------------------------------------------
# OpenRouter: fallback_models_override respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_chat_structured_respects_fallback_override() -> None:
    client = _openrouter_client()
    expected = _Fact(label="override", value="yes")
    comp = _mock_completion()
    seen_models: list[str] = []

    async def side_effect(**kwargs: object) -> tuple[_Fact, MagicMock]:
        seen_models.append(str(kwargs.get("model")))
        if kwargs.get("model") == "override-model-1":
            raise RuntimeError("fail")
        return (expected, comp)

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(side_effect=side_effect)
    client._instructor_async_client = mock_instructor

    result = await client.chat_structured(
        _MESSAGES,
        response_model=_Fact,
        fallback_models_override=["override-model-1", "override-model-2"],
    )

    assert result.model_used == "override-model-2"
    assert seen_models == ["override-model-1", "override-model-2"]


# ---------------------------------------------------------------------------
# OpenAI: success path (same pattern, different mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_chat_structured_success() -> None:
    client = _openai_client()
    expected = _Fact(label="gravity", value="9.81 m/s²")
    comp = _mock_completion(prompt_tokens=60, completion_tokens=15)

    mock_instructor = MagicMock()
    mock_instructor.chat.completions.create_with_completion = AsyncMock(
        return_value=(expected, comp)
    )
    client._instructor_async_client = mock_instructor

    result = await client.chat_structured(_MESSAGES, response_model=_Fact)

    assert result.parsed is expected
    assert result.tokens_prompt == 60
    assert result.tokens_completion == 15


# ---------------------------------------------------------------------------
# Anthropic: raises NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_chat_structured_not_implemented() -> None:
    client = AnthropicClient(_API_KEY)

    with pytest.raises(NotImplementedError, match="Anthropic"):
        await client.chat_structured(_MESSAGES, response_model=_Fact)


# ---------------------------------------------------------------------------
# StructuredLLMResult type checks
# ---------------------------------------------------------------------------


def test_structured_llm_result_fields() -> None:
    fact = _Fact(label="x", value="y")
    result: StructuredLLMResult[_Fact] = StructuredLLMResult(
        parsed=fact,
        tokens_prompt=10,
        tokens_completion=5,
        latency_ms=123,
        model_used="test",
    )
    assert result.parsed.label == "x"
    assert result.retry_count == 0
    assert result.latency_ms == 123
