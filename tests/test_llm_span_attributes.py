"""Unit tests for Phase 2 LLM telemetry: span attributes and retry-exhaustion counter.

Verifies:
- llm.chat span receives token, cost, latency, model_served, and
  models_attempted_count attributes after a successful call.
- llm.chat span receives fallback_rung_index=0 when the primary model answers.
- llm.chat span receives fallback_rung_index=1 when the first fallback answers.
- llm.chat_structured span receives model_served, cost_usd, token attributes
  after the call (restructured from bare return-await).
- record_llm_call_retry_exhaustion is called once when all cascade models fail.
- correlation_id is attached to both llm.chat and llm.chat_structured spans.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.observability.metrics as _metrics_mod
from app.adapters.openrouter.openrouter_client import OpenRouterClient, OpenRouterClientConfig
from app.core.call_status import CallStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    model: str = "primary/m0",
    fallback_models: tuple[str, ...] = ("fallback/m1",),
) -> OpenRouterClient:
    return OpenRouterClient(
        api_key="sk-or-test-key",
        model=model,
        fallback_models=fallback_models,
        config=OpenRouterClientConfig(max_retries=1),
    )


def _make_llm_result(
    *,
    status: CallStatus = CallStatus.OK,
    model: str = "primary/m0",
    tokens_prompt: int = 100,
    tokens_completion: int = 50,
    cost_usd: float = 0.00123,
    latency_ms: int = 420,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    models_attempted: list[tuple[str, str]] | None = None,
) -> Any:
    from app.adapter_models.llm.llm_models import LLMCallResult

    return LLMCallResult(
        status=status,
        model=model,
        response_text="ok",
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        models_attempted=models_attempted or [(model, "success")],
    )


def _make_structured_result(
    *,
    model_used: str = "primary/m0",
    tokens_prompt: int = 80,
    tokens_completion: int = 40,
    cost_usd: float = 0.00099,
    latency_ms: int = 310,
) -> Any:
    from app.adapter_models.llm.llm_models import StructuredLLMResult

    return StructuredLLMResult(
        parsed={"answer": 42},
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        model_used=model_used,
    )


# ---------------------------------------------------------------------------
# Span-recording test double
# ---------------------------------------------------------------------------


class _RecordingSpan:
    """Minimal span double that records set_attribute calls."""

    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}
        self._recording = True

    def is_recording(self) -> bool:
        return self._recording

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *_: Any) -> None:
        pass


class _RecordingTracer:
    """Minimal tracer double that injects a _RecordingSpan."""

    def __init__(self, span: _RecordingSpan) -> None:
        self._span = span

    def start_as_current_span(
        self, name: str, *, attributes: dict[str, Any] | None = None, **kwargs: Any
    ) -> _RecordingSpan:
        if attributes:
            for k, v in attributes.items():
                self._span.attrs[k] = v
        return self._span


# ---------------------------------------------------------------------------
# llm.chat span attribute tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_span_receives_token_attributes() -> None:
    """llm.chat span must carry tokens_prompt, tokens_completion, tokens_total."""
    from app.observability.attributes import (
        LLM_TOKENS_COMPLETION,
        LLM_TOKENS_PROMPT,
        LLM_TOKENS_TOTAL,
    )

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(tokens_prompt=120, tokens_completion=60)

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert span.attrs[LLM_TOKENS_PROMPT] == 120
    assert span.attrs[LLM_TOKENS_COMPLETION] == 60
    assert span.attrs[LLM_TOKENS_TOTAL] == 180


@pytest.mark.asyncio
async def test_chat_span_receives_model_served() -> None:
    """llm.chat span must carry model_served equal to result.model."""
    from app.observability.attributes import LLM_MODEL_SERVED

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(model="fallback/m1")

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert span.attrs[LLM_MODEL_SERVED] == "fallback/m1"


@pytest.mark.asyncio
async def test_chat_span_receives_cost_and_latency() -> None:
    """llm.chat span must carry cost_usd and latency_ms."""
    from app.observability.attributes import LLM_COST_USD, LLM_LATENCY_MS

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(cost_usd=0.00555, latency_ms=999)

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert span.attrs[LLM_COST_USD] == pytest.approx(0.00555)
    assert span.attrs[LLM_LATENCY_MS] == 999


@pytest.mark.asyncio
async def test_chat_span_fallback_rung_index_primary() -> None:
    """fallback_rung_index must be 0 when the primary model answered."""
    from app.observability.attributes import LLM_FALLBACK_RUNG_INDEX, LLM_MODELS_ATTEMPTED_COUNT

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(
        model="primary/m0",
        models_attempted=[("primary/m0", "success")],
    )

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert span.attrs[LLM_FALLBACK_RUNG_INDEX] == 0
    assert span.attrs[LLM_MODELS_ATTEMPTED_COUNT] == 1


@pytest.mark.asyncio
async def test_chat_span_fallback_rung_index_first_fallback() -> None:
    """fallback_rung_index must be 1 when the first fallback answered."""
    from app.observability.attributes import LLM_FALLBACK_RUNG_INDEX, LLM_MODELS_ATTEMPTED_COUNT

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(
        model="fallback/m1",
        models_attempted=[("primary/m0", "error"), ("fallback/m1", "success")],
    )

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert span.attrs[LLM_FALLBACK_RUNG_INDEX] == 1
    assert span.attrs[LLM_MODELS_ATTEMPTED_COUNT] == 2


@pytest.mark.asyncio
async def test_chat_span_cache_tokens_set_when_present() -> None:
    """Cache read/creation token attrs must appear when result carries them."""
    from app.observability.attributes import LLM_CACHE_CREATION_TOKENS, LLM_CACHE_READ_TOKENS

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(cache_read_tokens=200, cache_creation_tokens=50)

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert span.attrs[LLM_CACHE_READ_TOKENS] == 200
    assert span.attrs[LLM_CACHE_CREATION_TOKENS] == 50


@pytest.mark.asyncio
async def test_chat_span_cache_tokens_absent_when_none() -> None:
    """Cache token attrs must NOT appear when result.cache_read_tokens is None."""
    from app.observability.attributes import LLM_CACHE_CREATION_TOKENS, LLM_CACHE_READ_TOKENS

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result(cache_read_tokens=None, cache_creation_tokens=None)

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert LLM_CACHE_READ_TOKENS not in span.attrs
    assert LLM_CACHE_CREATION_TOKENS not in span.attrs


@pytest.mark.asyncio
async def test_chat_span_correlation_id_attached() -> None:
    """correlation_id must be set on the span when passed to chat()."""
    from app.observability.attributes import REQUEST_CORRELATION_ID

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result()

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat(
                [{"role": "user", "content": "hi"}],
                correlation_id="test-corr-id-abc123",
            )

    assert span.attrs[REQUEST_CORRELATION_ID] == "test-corr-id-abc123"


@pytest.mark.asyncio
async def test_chat_span_no_correlation_id_when_none() -> None:
    """correlation_id attribute must NOT be set when no correlation_id is given."""
    from app.observability.attributes import REQUEST_CORRELATION_ID

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    result = _make_llm_result()

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client.chat_engine, "chat", new=AsyncMock(return_value=result)):
            await client.chat([{"role": "user", "content": "hi"}])

    assert REQUEST_CORRELATION_ID not in span.attrs


# ---------------------------------------------------------------------------
# llm.chat_structured span attribute tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_structured_span_receives_model_served() -> None:
    """llm.chat_structured span must carry model_served from StructuredLLMResult."""
    from app.observability.attributes import LLM_MODEL_SERVED

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    s_result = _make_structured_result(model_used="fallback/m1")

    # _get_tracer is a local import alias inside chat_structured; patch the
    # source function in app.observability.otel so both local imports resolve
    # to the same double.
    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client, "_chat_structured_impl", new=AsyncMock(return_value=s_result)):
            await client.chat_structured(
                [{"role": "user", "content": "hi"}],
                response_model=MagicMock(),
            )

    assert span.attrs[LLM_MODEL_SERVED] == "fallback/m1"


@pytest.mark.asyncio
async def test_chat_structured_span_receives_token_and_cost_attributes() -> None:
    """llm.chat_structured span must carry token and cost attributes."""
    from app.observability.attributes import (
        LLM_COST_USD,
        LLM_TOKENS_COMPLETION,
        LLM_TOKENS_PROMPT,
        LLM_TOKENS_TOTAL,
    )

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    s_result = _make_structured_result(tokens_prompt=80, tokens_completion=40, cost_usd=0.00099)

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client, "_chat_structured_impl", new=AsyncMock(return_value=s_result)):
            await client.chat_structured(
                [{"role": "user", "content": "hi"}],
                response_model=MagicMock(),
            )

    assert span.attrs[LLM_TOKENS_PROMPT] == 80
    assert span.attrs[LLM_TOKENS_COMPLETION] == 40
    assert span.attrs[LLM_TOKENS_TOTAL] == 120
    assert span.attrs[LLM_COST_USD] == pytest.approx(0.00099)


@pytest.mark.asyncio
async def test_chat_structured_span_correlation_id_attached() -> None:
    """correlation_id must be set on the llm.chat_structured span."""
    from app.observability.attributes import REQUEST_CORRELATION_ID

    client = _make_client()
    span = _RecordingSpan()
    tracer = _RecordingTracer(span)
    s_result = _make_structured_result()

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with patch.object(client, "_chat_structured_impl", new=AsyncMock(return_value=s_result)):
            await client.chat_structured(
                [{"role": "user", "content": "hi"}],
                response_model=MagicMock(),
                correlation_id="corr-structured-xyz",
            )

    assert span.attrs[REQUEST_CORRELATION_ID] == "corr-structured-xyz"


# ---------------------------------------------------------------------------
# record_llm_call_retry_exhaustion wiring test
# ---------------------------------------------------------------------------


pytestmark_prometheus = pytest.mark.skipif(
    not _metrics_mod.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed",
)


def _counter_value(counter: Any, **labels: Any) -> float:
    return counter.labels(**labels)._value.get()


@pytest.mark.skipif(
    not _metrics_mod.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed",
)
def test_record_llm_call_retry_exhaustion_increments_counter() -> None:
    """record_llm_call_retry_exhaustion must increment LLM_CALL_RETRY_EXHAUSTION_TOTAL."""
    # Use a known-allowlisted model so the label is not bucketed to "other".
    from app.observability.metrics import _DEFAULT_MODEL_ALLOWLIST, LLM_CALL_RETRY_EXHAUSTION_TOTAL

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    before = _counter_value(LLM_CALL_RETRY_EXHAUSTION_TOTAL, model=model)

    _metrics_mod.record_llm_call_retry_exhaustion(model=model)

    after = _counter_value(LLM_CALL_RETRY_EXHAUSTION_TOTAL, model=model)
    assert after == before + 1.0


@pytest.mark.skipif(
    not _metrics_mod.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed",
)
def test_record_llm_call_retry_exhaustion_unknown_model_buckets_to_other() -> None:
    """Unknown model must be stored under 'other' label."""
    from app.observability.metrics import LLM_CALL_RETRY_EXHAUSTION_TOTAL

    before = _counter_value(LLM_CALL_RETRY_EXHAUSTION_TOTAL, model="other")
    _metrics_mod.record_llm_call_retry_exhaustion(model="totally/unknown-cascade-model-2099")
    after = _counter_value(LLM_CALL_RETRY_EXHAUSTION_TOTAL, model="other")
    assert after == before + 1.0


def test_record_llm_call_retry_exhaustion_is_noop_without_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must not raise when prometheus_client is absent."""
    monkeypatch.setattr(_metrics_mod, "PROMETHEUS_AVAILABLE", False)
    _metrics_mod.record_llm_call_retry_exhaustion(model="any/model")


# ---------------------------------------------------------------------------
# chat_engine.py exhaustion integration: counter fires on full cascade failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_engine_exhaustion_fires_retry_exhaustion_counter() -> None:
    """record_llm_call_retry_exhaustion must be called when all cascade models fail."""
    import asyncio
    from contextlib import asynccontextmanager

    from app.adapters.openrouter.chat_engine import OpenRouterChatEngine

    client = _make_client(model="primary/m0", fallback_models=("fallback/m1",))
    engine = OpenRouterChatEngine(client)

    @asynccontextmanager
    async def _fake_request_context() -> Any:
        yield MagicMock()

    client._request_context = _fake_request_context  # type: ignore[method-assign]

    async def _always_timeout(**kwargs: Any) -> Any:
        await asyncio.sleep(1.0)
        return MagicMock()

    engine._attempt_runner.run_attempts_for_model = _always_timeout  # type: ignore[method-assign]

    call_count: list[str] = []

    original = _metrics_mod.record_llm_call_retry_exhaustion

    def _spy(*, model: str) -> None:
        call_count.append(model)
        original(model=model)

    # chat_engine imports the helper by name, so patch it in that module's
    # namespace (patching app.observability.metrics would not intercept the call).
    with patch(
        "app.adapters.openrouter.chat_engine.record_llm_call_retry_exhaustion",
        side_effect=_spy,
    ):
        result = await engine.chat(
            messages=[{"role": "user", "content": "hi"}],
            per_model_timeout_sec=0.05,
        )

    assert result.status == CallStatus.ERROR
    # Must be called exactly once per request (not once per model).
    assert len(call_count) == 1
    # Must use the last model in the cascade.
    assert call_count[0] == "fallback/m1"
