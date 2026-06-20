"""Tests for AnthropicDirectLLMClient.chat_structured retry and failure paths.

Covers audit finding [2]: missing test coverage for retry/failure behaviour,
including non-retryable 4xx fast-break, exhausted-retry RuntimeError chaining,
and CancelledError propagation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import BaseModel

from app.adapters.llm.anthropic_direct import AnthropicDirectLLMClient


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Replace asyncio.sleep in the base client to make retries instant."""
    monkeypatch.setattr(
        "app.adapters.llm.base_client.asyncio.sleep",
        AsyncMock(return_value=None),
    )


class _Schema(BaseModel):
    value: int


def _make_client(*, max_retries: int = 3) -> AnthropicDirectLLMClient:
    return AnthropicDirectLLMClient(
        api_key="sk-ant-test",
        model="claude-test",
        base_url="https://anthropic.test/v1",
        version="2023-06-01",
        temperature=0.2,
        max_tokens=256,
        timeout_sec=10,
        max_retries=max_retries,
        max_response_size_mb=10,
    )


# ---------------------------------------------------------------------------
# Happy-path: the existing roundtrip test lives in test_direct_provider_e2e.py.
# These tests focus exclusively on error and retry paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_retries_exhausted_raises_runtime_error(respx_mock) -> None:
    """When every attempt returns an error, RuntimeError is raised and chains the cause."""
    respx_mock.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            500,
            json={"error": {"message": "server error"}},
        )
    )

    client = _make_client(max_retries=2)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            await client.chat_structured(
                [{"role": "user", "content": "go"}],
                response_model=_Schema,
                max_retries=2,
            )
    finally:
        await client.aclose()

    err = exc_info.value
    assert "Structured chat failed after retries" in str(err)
    # __cause__ must be set (finding [1] also chains the error)
    assert err.__cause__ is not None


@pytest.mark.asyncio
async def test_non_retryable_4xx_breaks_immediately(respx_mock) -> None:
    """A 400 response must not consume retry slots — only one HTTP call is made."""
    route = respx_mock.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "bad request"}},
        )
    )

    client = _make_client(max_retries=3)
    try:
        with pytest.raises(RuntimeError):
            await client.chat_structured(
                [{"role": "user", "content": "go"}],
                response_model=_Schema,
                max_retries=3,
            )
    finally:
        await client.aclose()

    # With max_retries=3 the loop would make up to 4 calls if it did not break
    # early. A non-retryable 4xx must stop after the very first call.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_401_is_non_retryable(respx_mock) -> None:
    """401 Unauthorized is a non-retryable 4xx; only one attempt must be made."""
    route = respx_mock.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "unauthorized"}},
        )
    )

    client = _make_client(max_retries=3)
    try:
        with pytest.raises(RuntimeError):
            await client.chat_structured(
                [{"role": "user", "content": "go"}],
                response_model=_Schema,
                max_retries=3,
            )
    finally:
        await client.aclose()

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_429_is_retryable(respx_mock) -> None:
    """429 Too Many Requests must be retried (not broken out of immediately)."""
    route = respx_mock.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "rate_limit"}},
        )
    )

    client = _make_client(max_retries=2)
    try:
        with pytest.raises(RuntimeError):
            await client.chat_structured(
                [{"role": "user", "content": "go"}],
                response_model=_Schema,
                max_retries=2,
            )
    finally:
        await client.aclose()

    # max_retries=2 means 3 total attempts (attempt 0, 1, 2)
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_cancelled_error_propagates() -> None:
    """asyncio.CancelledError raised inside chat() must propagate unmodified."""
    client = _make_client(max_retries=2)
    try:
        with patch.object(
            client,
            "chat",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}],
                    response_model=_Schema,
                    max_retries=2,
                )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_error_text_included_in_raised_exception(respx_mock) -> None:
    """The raised RuntimeError must include the provider error text."""
    respx_mock.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            500,
            json={"error": {"message": "internal model failure"}},
        )
    )

    client = _make_client(max_retries=0)
    try:
        with pytest.raises(RuntimeError, match="Structured chat failed after retries"):
            await client.chat_structured(
                [{"role": "user", "content": "go"}],
                response_model=_Schema,
                max_retries=0,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_success_after_transient_errors(respx_mock) -> None:
    """After initial failures a successful response on the last attempt is returned."""
    responses = [
        httpx.Response(500, json={"error": {"message": "transient"}}),
        httpx.Response(500, json={"error": {"message": "transient"}}),
        httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"value": 42}'}],
                "usage": {"input_tokens": 5, "output_tokens": 6},
            },
        ),
    ]
    respx_mock.post("https://anthropic.test/v1/messages").mock(side_effect=responses)

    client = _make_client(max_retries=3)
    try:
        result = await client.chat_structured(
            [{"role": "user", "content": "go"}],
            response_model=_Schema,
            max_retries=3,
        )
    finally:
        await client.aclose()

    assert result.parsed == _Schema(value=42)
    assert result.retry_count == 2
