from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from app.adapters.llm.factory import LLMClientFactory
from app.config import DirectAnthropicConfig, DirectOllamaConfig, DirectOpenAIConfig
from app.config.runtime import RuntimeConfig
from tests.conftest import make_test_app_config


class ProviderSummary(BaseModel):
    title: str
    score: int


@pytest.mark.asyncio
async def test_direct_openai_provider_structured_roundtrip(respx_mock) -> None:
    route = respx_mock.post("https://openai.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"title":"openai ok","score":1}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8},
            },
        )
    )
    cfg = make_test_app_config(
        runtime=RuntimeConfig(llm_provider="openai"),
        openai=DirectOpenAIConfig(
            api_key="sk-openai-test",
            model="gpt-4o-mini",
            base_url="https://openai.test/v1",
        ),
    )

    client = LLMClientFactory.create_from_config(cfg)
    try:
        result = await client.chat_structured(
            [{"role": "user", "content": "Summarize"}],
            response_model=ProviderSummary,
        )
    finally:
        await client.aclose()

    assert result.parsed == ProviderSummary(title="openai ok", score=1)
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-openai-test"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content)["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_direct_anthropic_provider_structured_roundtrip(respx_mock) -> None:
    route = respx_mock.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"title":"anthropic ok","score":2}'}],
                "usage": {"input_tokens": 9, "output_tokens": 10},
            },
        )
    )
    cfg = make_test_app_config(
        runtime=RuntimeConfig(llm_provider="anthropic"),
        anthropic=DirectAnthropicConfig(
            api_key="sk-ant-test",
            model="claude-sonnet-4-5",
            base_url="https://anthropic.test/v1",
        ),
    )

    client = LLMClientFactory.create_from_config(cfg)
    try:
        result = await client.chat_structured(
            [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "Summarize"},
            ],
            response_model=ProviderSummary,
        )
    finally:
        await client.aclose()

    assert result.parsed == ProviderSummary(title="anthropic ok", score=2)
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    payload = json.loads(request.content)
    assert payload["system"] == "Return JSON only."
    assert payload["messages"] == [{"role": "user", "content": "Summarize"}]


@pytest.mark.asyncio
async def test_direct_ollama_provider_structured_roundtrip(respx_mock) -> None:
    route = respx_mock.post("http://ollama.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"title":"ollama ok","score":3}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 12},
            },
        )
    )
    cfg = make_test_app_config(
        runtime=RuntimeConfig(llm_provider="ollama"),
        ollama=DirectOllamaConfig(model="llama3.2", base_url="http://ollama.test/v1"),
    )

    client = LLMClientFactory.create_from_config(cfg)
    try:
        result = await client.chat_structured(
            [{"role": "user", "content": "Summarize"}],
            response_model=ProviderSummary,
        )
    finally:
        await client.aclose()

    assert result.parsed == ProviderSummary(title="ollama ok", score=3)
    request = route.calls.last.request
    assert "authorization" not in request.headers
    assert json.loads(request.content)["model"] == "llama3.2"
