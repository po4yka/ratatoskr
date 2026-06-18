"""Test OpenRouter API compliance according to official documentation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from app.adapters.openrouter.openrouter_client import OpenRouterClient, OpenRouterClientConfig

OR_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_MODELS_URL = "https://openrouter.ai/api/v1/models"

_SUCCESS_JSON = {
    "choices": [{"message": {"content": "Test response"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


@pytest_asyncio.fixture
async def or_client():
    from app.adapters.openrouter.model_capabilities import ModelCapabilities

    ModelCapabilities._structured_models = None

    with patch(
        "app.adapters.openrouter.model_capabilities.ModelCapabilities._fetch_structured_models",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = {"qwen/qwen3-max"}
        client = OpenRouterClient(
            api_key="sk-or-test-key",
            model="qwen/qwen3-max",
            fallback_models=["google/gemini-3.1-pro-preview"],
            http_referer="https://github.com/test-repo",
            x_title="Test Bot",
            config=OpenRouterClientConfig(timeout_sec=30, max_retries=2, debug_payloads=True),
        )
        yield client
        await client.aclose()


@pytest.mark.asyncio
async def test_correct_api_endpoint(respx_mock, or_client):
    route = respx_mock.post(OR_CHAT_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_JSON))
    await or_client.chat([{"role": "user", "content": "Hello"}])
    assert route.called
    assert route.calls[0].request.url.path == "/api/v1/chat/completions"


@pytest.mark.asyncio
async def test_authentication_header(respx_mock, or_client):
    route = respx_mock.post(OR_CHAT_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_JSON))
    await or_client.chat([{"role": "user", "content": "Hello"}])
    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-or-test-key"


@pytest.mark.asyncio
async def test_request_structure(respx_mock, or_client):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the meaning of life?"},
    ]
    route = respx_mock.post(OR_CHAT_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_JSON))
    await or_client.chat(messages, temperature=0.7, max_tokens=100)
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen/qwen3-max"
    assert body["messages"] == messages
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 100


@pytest.mark.asyncio
async def test_optional_parameters(respx_mock, or_client):
    sse_body = (
        b'data: {"model":"qwen/qwen3-max","choices":[{"index":0,'
        b'"delta":{"content":"Test response"},"finish_reason":null}]}\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5}}\n'
        b"data: [DONE]\n"
        b"\n"
    )
    route = respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )
    )
    await or_client.chat(
        [{"role": "user", "content": "Hello"}],
        temperature=0.5,
        max_tokens=50,
        top_p=0.9,
        stream=True,
    )
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 50
    assert body["top_p"] == 0.9
    assert body["stream"]


@pytest.mark.asyncio
async def test_http_headers(respx_mock, or_client):
    route = respx_mock.post(OR_CHAT_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_JSON))
    await or_client.chat([{"role": "user", "content": "Hello"}])
    assert route.called
    headers = route.calls[0].request.headers
    assert headers["content-type"] == "application/json"
    assert headers["http-referer"] == "https://github.com/test-repo"
    assert headers["x-title"] == "Test Bot"


@pytest.mark.asyncio
async def test_error_handling_400(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "Invalid request parameters"}},
        )
    )
    result = await or_client.chat([{"role": "user", "content": "Hello"}])
    assert result.status == "error"
    assert "Invalid or missing request parameters" in result.error_text


@pytest.mark.asyncio
async def test_error_handling_401(respx_mock, or_client):
    token = "sk-or-test-key"
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "Invalid API key"}},
        )
    )
    result = await or_client.chat([{"role": "user", "content": "Hello"}])
    assert result.status == "error"
    assert "Authentication failed" in result.error_text
    assert result.request_headers["Authorization"] == "[REDACTED]"
    assert token not in str(result.request_headers)
    assert token not in (result.error_text or "")


@pytest.mark.asyncio
async def test_error_handling_402(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            402,
            json={"error": {"message": "Insufficient credits"}},
        )
    )
    result = await or_client.chat([{"role": "user", "content": "Hello"}])
    assert result.status == "error"
    assert "Insufficient account balance" in result.error_text


@pytest.mark.asyncio
async def test_error_handling_404(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            404,
            json={"error": {"message": "Model not found"}},
        )
    )
    result = await or_client.chat([{"role": "user", "content": "Hello"}])
    assert result.status == "error"
    assert "Requested resource not found" in result.error_text


@pytest.mark.asyncio
async def test_error_handling_429_with_retry_after(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded"}},
            headers={"retry-after": "5"},
        )
    )
    with patch("asyncio.sleep") as mock_sleep:
        await or_client.chat([{"role": "user", "content": "Hello"}])
        mock_sleep.assert_called_with(5)


@pytest.mark.asyncio
async def test_error_handling_500(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            500,
            json={"error": {"message": "Internal server error"}},
        )
    )
    with patch("asyncio.sleep") as mock_sleep:
        await or_client.chat([{"role": "user", "content": "Hello"}])
        assert mock_sleep.call_count > 0


@pytest.mark.asyncio
async def test_success_response_parsing(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Test response"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "deepseek/deepseek-v4-flash",
            },
        )
    )
    result = await or_client.chat([{"role": "user", "content": "Hello"}])
    assert result.status == "ok"
    assert result.response_text == "Test response"
    assert result.tokens_prompt == 10
    assert result.tokens_completion == 5
    assert result.model == "deepseek/deepseek-v4-flash"
    assert result.endpoint == "/api/v1/chat/completions"


@pytest.mark.asyncio
async def test_structured_output_content_with_json_part(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "test-response",
                "model": "qwen/qwen3-max",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "reasoning", "text": "Planning structured output"},
                                {
                                    "type": "output_json",
                                    "json": {
                                        "summary_250": "Short summary",
                                        "summary_1000": "Medium summary",
                                        "tldr": "Longer summary",
                                    },
                                },
                            ],
                        },
                        "finish_reason": "stop",
                        "native_finish_reason": "completed",
                    }
                ],
            },
        )
    )
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "summary_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "summary_250": {"type": "string"},
                    "summary_1000": {"type": "string"},
                    "tldr": {"type": "string"},
                },
                "required": ["summary_250", "summary_1000", "tldr"],
            },
        },
    }
    result = await or_client.chat(
        [{"role": "user", "content": "Hello"}],
        response_format=response_format,
    )
    assert result.status == "ok"
    assert result.response_text is not None
    parsed = json.loads(result.response_text or "{}")
    assert parsed["summary_250"] == "Short summary"
    assert parsed["summary_1000"] == "Medium summary"
    assert parsed["tldr"] == "Longer summary"


@pytest.mark.asyncio
async def test_models_endpoint(respx_mock, or_client):
    respx_mock.get(OR_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V3"},
                    {"id": "google/gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview"},
                ]
            },
        )
    )
    models = await or_client.get_models()
    assert respx_mock.calls[-1].request.url == OR_MODELS_URL
    assert "data" in models
    assert len(models["data"]) == 2


@pytest.mark.asyncio
async def test_fallback_models(respx_mock, or_client):
    respx_mock.post(OR_CHAT_URL).mock(
        side_effect=[
            httpx.Response(500, json={"error": "Server error"}),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Fallback response"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "model": "google/gemini-3.1-pro-preview",
                },
            ),
        ]
    )
    with patch("asyncio.sleep"):
        result = await or_client.chat([{"role": "user", "content": "Hello"}])
    assert result.status == "ok"
    assert result.response_text == "Fallback response"
    assert result.model == "google/gemini-3.1-pro-preview"


def _make_sync_client() -> OpenRouterClient:
    """Create a fresh client for sync tests that use asyncio.run()."""
    from app.adapters.openrouter.model_capabilities import ModelCapabilities

    ModelCapabilities._structured_models = None
    return OpenRouterClient(
        api_key="sk-or-test-key",
        model="qwen/qwen3-max",
        config=OpenRouterClientConfig(timeout_sec=30, max_retries=0),
    )


def test_parameter_validation():
    from pydantic_core import ValidationError as PydanticValidationError

    from app.adapters.openrouter.exceptions import ValidationError

    client = _make_sync_client()
    try:
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}], temperature=3.0))
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}], max_tokens=-1))
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}], top_p=1.5))
        with pytest.raises(PydanticValidationError):
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}], stream="true"))
    finally:
        asyncio.run(client.aclose())


def test_message_validation():
    from app.adapters.openrouter.exceptions import ValidationError

    client = _make_sync_client()
    try:
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([]))
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([{"role": "user"}]))
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([{"role": "invalid", "content": "Hello"}]))
        with pytest.raises(ValidationError):
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}] * 51))
    finally:
        asyncio.run(client.aclose())


def test_error_message_generation():
    client = _make_sync_client()
    try:
        error_msg = client._get_error_message(400, {"error": {"message": "Bad request"}})
        assert "Invalid or missing request parameters" in error_msg
        assert "Bad request" in error_msg

        error_msg = client._get_error_message(401, {"error": "Unauthorized"})
        assert "Authentication failed" in error_msg
        assert "Unauthorized" in error_msg

        error_msg = client._get_error_message(402, {})
        assert "Insufficient account balance" in error_msg

        error_msg = client._get_error_message(404, {})
        assert "Requested resource not found" in error_msg

        error_msg = client._get_error_message(429, {})
        assert "Rate limit exceeded" in error_msg

        error_msg = client._get_error_message(500, {})
        assert "Internal server error" in error_msg

        error_msg = client._get_error_message(999, {})
        assert "HTTP 999 error" in error_msg
    finally:
        import asyncio

        asyncio.run(client.aclose())
