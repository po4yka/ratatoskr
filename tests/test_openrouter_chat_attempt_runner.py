from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapter_models.llm.llm_models import ChatRequest, LLMCallResult
from app.adapters.openrouter.chat_attempt_runner import ChatAttemptRunner
from app.adapters.openrouter.chat_models import (
    AttemptOutcome,
    RetryDirective,
    StructuredOutputState,
    TruncationRecovery,
)
from app.adapters.openrouter.openrouter_client import OpenRouterClient, OpenRouterClientConfig
from app.core.call_status import CallStatus


def _make_client() -> OpenRouterClient:
    return OpenRouterClient(
        api_key="sk-or-test-key",
        model="qwen/qwen3-max",
        config=OpenRouterClientConfig(max_retries=2),
    )


def _make_request(*, stream: bool = True, max_tokens: int | None = 100) -> ChatRequest:
    return ChatRequest(
        messages=[{"role": "user", "content": "hello"}],
        temperature=0.2,
        max_tokens=max_tokens,
        top_p=None,
        stream=stream,
        request_id=5,
        response_format={"type": "json_object"},
        model_override=None,
    )


@pytest.mark.asyncio
async def test_chat_attempt_runner_retries_with_backoff_and_returns_success() -> None:
    client = _make_client()
    client.error_handler.sleep_backoff = AsyncMock()
    transport = MagicMock()
    transport.attempt_request = AsyncMock(
        side_effect=[
            AttemptOutcome(
                retry=RetryDirective(
                    rf_mode="json_schema",
                    response_format={"type": "json_object"},
                    backoff_needed=True,
                ),
                structured_output_state=StructuredOutputState(used=True, mode="json_schema"),
            ),
            AttemptOutcome(
                success=True,
                llm_result=LLMCallResult(status=CallStatus.OK, model="qwen/qwen3-max"),
                structured_output_state=StructuredOutputState(used=True, mode="json_object"),
            ),
        ]
    )
    runner = ChatAttemptRunner(client, transport)

    state = await runner.run_attempts_for_model(
        client=MagicMock(),
        model="qwen/qwen3-max",
        request=_make_request(),
        sanitized_messages=[{"role": "user", "content": "hello"}],
        message_lengths=[5],
        message_roles=["user"],
        total_chars=5,
        request_id=5,
        initial_rf_mode="json_schema",
        response_format_initial={"type": "json_object"},
        structured_output_state=StructuredOutputState(),
    )

    assert state.terminal_result is not None
    assert state.terminal_result.status == "ok"
    client.error_handler.sleep_backoff.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_attempt_runner_falls_back_from_stream_to_non_stream() -> None:
    client = _make_client()
    transport = MagicMock()
    transport.attempt_request = AsyncMock(
        side_effect=[
            AttemptOutcome(
                retry=RetryDirective(
                    rf_mode="json_schema",
                    response_format={"type": "json_object"},
                    backoff_needed=False,
                    fallback_to_non_stream=True,
                ),
                structured_output_state=StructuredOutputState(used=True, mode="json_schema"),
            ),
            AttemptOutcome(
                success=True,
                llm_result=LLMCallResult(status=CallStatus.OK, model="qwen/qwen3-max"),
                structured_output_state=StructuredOutputState(used=True, mode="json_schema"),
            ),
        ]
    )
    runner = ChatAttemptRunner(client, transport)

    state = await runner.run_attempts_for_model(
        client=MagicMock(),
        model="qwen/qwen3-max",
        request=_make_request(stream=True),
        sanitized_messages=[{"role": "user", "content": "hello"}],
        message_lengths=[5],
        message_roles=["user"],
        total_chars=5,
        request_id=7,
        initial_rf_mode="json_schema",
        response_format_initial={"type": "json_object"},
        structured_output_state=StructuredOutputState(),
    )

    assert state.terminal_result is not None
    assert transport.attempt_request.await_args_list[1].kwargs["request"].stream is False


@pytest.mark.asyncio
async def test_chat_attempt_runner_stops_after_repeated_truncation() -> None:
    client = _make_client()
    transport = MagicMock()
    transport.attempt_request = AsyncMock(
        side_effect=[
            AttemptOutcome(
                retry=RetryDirective(
                    rf_mode="json_schema",
                    response_format={"type": "json_object"},
                    backoff_needed=False,
                    truncation_recovery=TruncationRecovery(
                        original_max_tokens=100,
                        suggested_max_tokens=200,
                    ),
                ),
                structured_output_state=StructuredOutputState(used=True, mode="json_schema"),
            ),
            AttemptOutcome(
                retry=RetryDirective(
                    rf_mode="json_schema",
                    response_format={"type": "json_object"},
                    backoff_needed=False,
                    truncation_recovery=TruncationRecovery(
                        original_max_tokens=200,
                        suggested_max_tokens=300,
                    ),
                ),
                structured_output_state=StructuredOutputState(used=True, mode="json_schema"),
            ),
        ]
    )
    runner = ChatAttemptRunner(client, transport)

    state = await runner.run_attempts_for_model(
        client=MagicMock(),
        model="qwen/qwen3-max",
        request=_make_request(stream=False, max_tokens=100),
        sanitized_messages=[{"role": "user", "content": "hello"}],
        message_lengths=[5],
        message_roles=["user"],
        total_chars=5,
        request_id=9,
        initial_rf_mode="json_schema",
        response_format_initial={"type": "json_object"},
        structured_output_state=StructuredOutputState(),
    )

    assert state.last_error_text == "repeated_truncation"
    assert state.request.max_tokens == 200


def test_chat_attempt_runner_builds_exhausted_parse_error_result() -> None:
    client = _make_client()
    transport = MagicMock()
    runner = ChatAttemptRunner(client, transport)

    result = runner.build_exhausted_chat_result(
        last_model_reported="qwen/qwen3-max",
        last_response_text="{bad json",
        last_data={"bad": True},
        last_latency=42,
        last_error_text="ignored",
        last_error_context={"message": "parse"},
        sanitized_messages=[{"role": "user", "content": "hello"}],
        structured_output_state=StructuredOutputState(
            used=True,
            mode="json_schema",
            parse_error=True,
        ),
    )

    assert result.status == "error"
    assert result.error_text == "structured_output_parse_error"
    assert result.retry_exhausted is True


def test_chat_attempt_runner_builds_exhausted_retry_budget_result() -> None:
    client = _make_client()
    transport = MagicMock()
    runner = ChatAttemptRunner(client, transport)

    result = runner.build_exhausted_chat_result(
        last_model_reported="qwen/qwen3-max",
        last_response_text=None,
        last_data={"error": {"message": "busy"}},
        last_latency=42,
        last_error_text="All retries failed",
        last_error_context={"message": "busy"},
        sanitized_messages=[{"role": "user", "content": "hello"}],
        structured_output_state=StructuredOutputState(),
        total_latency_ms=84,
    )

    assert result.status == "error"
    assert result.retry_exhausted is True
    assert result.total_latency_ms == 84
