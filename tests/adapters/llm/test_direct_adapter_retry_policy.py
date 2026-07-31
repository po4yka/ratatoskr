"""Retry policy and per-attempt telemetry for the direct LLM adapters.

These paths run whenever LLM_PROVIDER is openai, anthropic or ollama. Each of
them used to fail in a way that cost real money without leaving a trace: a
timeout gave up after one call while a hopeless 400 burned four, retries fired
back to back with no pause, and every attempt past the winner was absent from
llm_calls -- so the provider bill and the database disagreed by up to 4x.

The loop is shared by both adapters on purpose. It used to be duplicated, and
commit 570e7498 fixed the non-retryable-4xx break in only one of the copies.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from app.adapters.llm.anthropic_direct import AnthropicDirectLLMClient
from app.adapters.llm.openai_compatible import OpenAICompatibleLLMClient

OPENAI_URL = "https://openai.test/v1/chat/completions"
ANTHROPIC_URL = "https://anthropic.test/v1/messages"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the assertions about *whether* we sleep, without the wall clock."""
    monkeypatch.setattr(
        "app.adapters.llm.base_client.sleep_backoff",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr("app.adapters.llm.base_client.asyncio.sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("app.core.backoff.asyncio.sleep", AsyncMock(return_value=None))


class _Schema(BaseModel):
    value: int


def _openai(*, max_retries: int = 3, **over: Any) -> OpenAICompatibleLLMClient:
    kwargs: dict[str, Any] = {
        "provider_name": "openai",
        "api_key": "sk-test",
        "model": "gpt-test",
        "base_url": "https://openai.test/v1",
        "temperature": 0.2,
        "max_tokens": 256,
        "timeout_sec": 10,
        "max_retries": max_retries,
        "max_response_size_mb": 10,
    }
    kwargs.update(over)
    return OpenAICompatibleLLMClient(**kwargs)


def _anthropic(*, max_retries: int = 3, **over: Any) -> AnthropicDirectLLMClient:
    kwargs: dict[str, Any] = {
        "api_key": "sk-ant-test",
        "model": "claude-test",
        "base_url": "https://anthropic.test/v1",
        "version": "2023-06-01",
        "temperature": 0.2,
        "max_tokens": 256,
        "timeout_sec": 10,
        "max_retries": max_retries,
        "max_response_size_mb": 10,
    }
    kwargs.update(over)
    return AnthropicDirectLLMClient(**kwargs)


def _openai_ok(
    text: str = '{"value": 42}', *, prompt: int = 5, completion: int = 7
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


def _anthropic_ok(text: str = '"value": 42}') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        },
    )


class TestNonRetryableStatuses:
    """A rotated key stays rotated; retrying only multiplies the bill."""

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    @pytest.mark.asyncio
    async def test_openai_breaks_immediately(self, respx_mock: Any, status: int) -> None:
        route = respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(status, json={"error": {"message": "nope"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
                )
        finally:
            await client.aclose()
        assert route.call_count == 1

    @pytest.mark.asyncio
    async def test_openai_still_retries_429(self, respx_mock: Any) -> None:
        route = respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=2
                )
        finally:
            await client.aclose()
        assert route.call_count == 3

    @pytest.mark.asyncio
    async def test_openai_still_retries_5xx(self, respx_mock: Any) -> None:
        route = respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "overloaded"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=2
                )
        finally:
            await client.aclose()
        assert route.call_count == 3


class TestTransportFailuresAreRetried:
    """The most retryable class of failure used to get the fewest retries."""

    @pytest.mark.asyncio
    async def test_a_timeout_is_retried_then_succeeds(self, respx_mock: Any) -> None:
        route = respx_mock.post(OPENAI_URL).mock(
            side_effect=[httpx.ConnectTimeout("timed out"), _openai_ok()]
        )
        client = _openai()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert result.parsed == _Schema(value=42)
        assert route.call_count == 2

    @pytest.mark.asyncio
    async def test_a_connect_error_is_retried(self, respx_mock: Any) -> None:
        route = respx_mock.post(ANTHROPIC_URL).mock(
            side_effect=[httpx.ConnectError("refused"), _anthropic_ok()]
        )
        client = _anthropic()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert result.parsed == _Schema(value=42)
        assert route.call_count == 2

    @pytest.mark.asyncio
    async def test_persistent_transport_failure_exhausts_the_budget(self, respx_mock: Any) -> None:
        route = respx_mock.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("refused"))
        client = _openai()
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=2
                )
        finally:
            await client.aclose()
        assert route.call_count == 3


class TestBackoff:
    @pytest.mark.asyncio
    async def test_retries_actually_wait(self, respx_mock: Any, monkeypatch: Any) -> None:
        """Four back-to-back POSTs in ~10 ms is what a 429 used to buy."""
        slept: list[int] = []

        async def _record(attempt: int, **_kw: Any) -> None:
            slept.append(attempt)

        monkeypatch.setattr("app.adapters.llm.base_client.sleep_backoff", _record)
        respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "boom"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=2
                )
        finally:
            await client.aclose()
        assert slept == [0, 1]

    @pytest.mark.asyncio
    async def test_no_sleep_after_the_final_attempt(
        self, respx_mock: Any, monkeypatch: Any
    ) -> None:
        slept: list[int] = []

        async def _record(attempt: int, **_kw: Any) -> None:
            slept.append(attempt)

        monkeypatch.setattr("app.adapters.llm.base_client.sleep_backoff", _record)
        respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "boom"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=0
                )
        finally:
            await client.aclose()
        assert slept == []

    @pytest.mark.asyncio
    async def test_retry_after_header_is_honored(self, respx_mock: Any, monkeypatch: Any) -> None:
        waited: list[float] = []

        async def _record(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr("app.adapters.llm.base_client.asyncio.sleep", _record)
        respx_mock.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(
                    429, json={"error": {"message": "slow"}}, headers={"retry-after": "7"}
                ),
                _openai_ok(),
            ]
        )
        client = _openai()
        try:
            await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert waited == [7.0]

    @pytest.mark.asyncio
    async def test_a_hostile_retry_after_is_capped(self, respx_mock: Any, monkeypatch: Any) -> None:
        """An unbounded sleep would park the summarize graph indefinitely."""
        waited: list[float] = []

        async def _record(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr("app.adapters.llm.base_client.asyncio.sleep", _record)
        respx_mock.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(
                    429, json={"error": {"message": "slow"}}, headers={"retry-after": "99999"}
                ),
                _openai_ok(),
            ]
        )
        client = _openai()
        try:
            await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert waited == [OpenAICompatibleLLMClient._RETRY_AFTER_MAX_SEC]


class TestPhysicalAttempts:
    """Every billed request must reach llm_calls -- CLAUDE.md rule 3."""

    @pytest.mark.asyncio
    async def test_success_after_retries_records_the_failed_calls(self, respx_mock: Any) -> None:
        respx_mock.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(429, json={"error": {"message": "slow down"}}),
                httpx.Response(503, json={"error": {"message": "overloaded"}}),
                _openai_ok(),
            ]
        )
        client = _openai()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()

        assert [a["status"] for a in result.physical_attempts] == ["error", "error", "ok"]
        assert "slow down" in (result.physical_attempts[0]["error_text"] or "")
        assert "overloaded" in (result.physical_attempts[1]["error_text"] or "")
        assert result.physical_attempts[-1]["tokens_prompt"] == 5
        assert result.retry_count == 2

    @pytest.mark.asyncio
    async def test_total_failure_attaches_attempts_to_the_exception(self, respx_mock: Any) -> None:
        """Bare RuntimeError meant graph_llm wrote zero rows for a failed call."""
        respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "overloaded"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError) as caught:
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=2
                )
        finally:
            await client.aclose()

        attempts = caught.value.__llm_physical_attempts__
        assert len(attempts) == 3
        assert {a["status"] for a in attempts} == {"error"}
        assert caught.value.__llm_result__ is not None
        assert caught.value.__cause__ is not None

    @pytest.mark.asyncio
    async def test_graph_llm_turns_the_attempts_into_rows(self, respx_mock: Any) -> None:
        """The consumer contract: one llm_calls row per physical request."""
        from app.application.services.summarization.graph_llm import (
            _exception_physical_attempts,
            _physical_attempts,
        )

        respx_mock.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(503, json={"error": {"message": "boom"}}),
                _openai_ok(),
            ]
        )
        client = _openai()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert len(_physical_attempts(result, terminal_status="ok")) == 2

        respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "boom"}})
        )
        client = _openai()
        try:
            with pytest.raises(RuntimeError) as caught:
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=1
                )
        finally:
            await client.aclose()
        assert len(_exception_physical_attempts(caught.value)) == 2

    @pytest.mark.asyncio
    async def test_transport_failures_are_recorded_too(self, respx_mock: Any) -> None:
        respx_mock.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("refused"))
        client = _openai()
        try:
            with pytest.raises(RuntimeError) as caught:
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=1
                )
        finally:
            await client.aclose()
        attempts = caught.value.__llm_physical_attempts__
        assert len(attempts) == 2
        assert all("refused" in (a["error_text"] or "") for a in attempts)


class TestCostAccounting:
    """A NULL cost makes SUM(cost_usd) zero, so the USD hard stop never fires."""

    @pytest.mark.asyncio
    async def test_configured_prices_produce_a_cost(self, respx_mock: Any) -> None:
        respx_mock.post(OPENAI_URL).mock(return_value=_openai_ok(prompt=1000, completion=2000))
        client = _openai(price_input_per_1k=0.01, price_output_per_1k=0.03)
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=0
            )
        finally:
            await client.aclose()
        assert result.cost_usd == pytest.approx(0.01 + 0.06)
        assert result.physical_attempts[0]["cost_usd"] == pytest.approx(0.07)

    @pytest.mark.asyncio
    async def test_cost_is_none_without_configured_prices(self, respx_mock: Any) -> None:
        respx_mock.post(OPENAI_URL).mock(return_value=_openai_ok())
        client = _openai()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=0
            )
        finally:
            await client.aclose()
        assert result.cost_usd is None


class TestProviderRetryCeiling:
    @pytest.mark.asyncio
    async def test_provider_max_retries_caps_the_callers_budget(self, respx_mock: Any) -> None:
        """OLLAMA_MAX_RETRIES=1 stops a slow local model from being hammered.

        The knob was read from the environment, stored, and never consulted --
        only the dead _run_with_retry ever looked at it.
        """
        route = respx_mock.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "boom"}})
        )
        client = _openai(max_retries=1)
        try:
            with pytest.raises(RuntimeError):
                await client.chat_structured(
                    [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=5
                )
        finally:
            await client.aclose()
        assert route.call_count == 2


class TestPayloadTolerance:
    @pytest.mark.asyncio
    async def test_a_fenced_reply_is_recovered(self, respx_mock: Any) -> None:
        """Strict json.loads made a code fence an unrecoverable summarize failure."""
        route = respx_mock.post(OPENAI_URL).mock(
            return_value=_openai_ok('```json\n{"value": 42}\n```')
        )
        client = _openai()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert result.parsed == _Schema(value=42)
        assert route.call_count == 1

    @pytest.mark.asyncio
    async def test_an_empty_choices_list_does_not_crash(self, respx_mock: Any) -> None:
        """Ollama answers 200 with choices=[]; indexing it raised IndexError.

        The raise happened outside _request_context, so it escaped every caller
        of chat(), not only the retry loop.
        """
        respx_mock.post(OPENAI_URL).mock(
            side_effect=[httpx.Response(200, json={"choices": [], "usage": {}}), _openai_ok()]
        )
        client = _openai()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=3
            )
        finally:
            await client.aclose()
        assert result.parsed == _Schema(value=42)

    @pytest.mark.asyncio
    async def test_anthropic_prefill_reply_is_reassembled(self, respx_mock: Any) -> None:
        """The prefilled brace is not echoed back, so the reply is one short."""
        respx_mock.post(ANTHROPIC_URL).mock(return_value=_anthropic_ok('"value": 42}'))
        client = _anthropic()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=0
            )
        finally:
            await client.aclose()
        assert result.parsed == _Schema(value=42)

    @pytest.mark.asyncio
    async def test_anthropic_whole_object_reply_is_left_alone(self, respx_mock: Any) -> None:
        """A model that ignores the prefill returns the full object."""
        respx_mock.post(ANTHROPIC_URL).mock(return_value=_anthropic_ok('{"value": 42}'))
        client = _anthropic()
        try:
            result = await client.chat_structured(
                [{"role": "user", "content": "go"}], response_model=_Schema, max_retries=0
            )
        finally:
            await client.aclose()
        assert result.parsed == _Schema(value=42)
