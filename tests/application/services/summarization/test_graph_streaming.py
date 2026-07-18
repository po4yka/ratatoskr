"""``summarize_streaming`` (ADR-0017): one chat(stream=True) call whose deltas
feed the on_token callback, parsed to a summary with parity post-processing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.application.graphs.summarize.nodes.validate import validate
from app.application.services.summarization.graph_llm import summarize_streaming
from app.core.call_status import CallStatus

_MESSAGES = [{"role": "user", "content": "x"}]


class _FakeLLM:
    """Records chat kwargs; streams text chunks to on_stream_delta then returns."""

    def __init__(
        self,
        *,
        response_json: dict[str, Any] | None = None,
        response_text: str = "",
        chunks: list[str] | None = None,
        status: CallStatus = CallStatus.OK,
        error_text: str | None = None,
    ) -> None:
        self._rj = response_json
        self._rt = response_text
        self._chunks = chunks or ([response_text] if response_text else [])
        self._status = status
        self._err = error_text
        self.received_deltas: list[str] = []
        self.chat_kwargs: dict[str, Any] = {}

    @property
    def provider_name(self) -> str:
        return "fake"

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
        on_stream_delta: Any = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> Any:
        kw = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "stream": stream,
            "request_id": request_id,
            "response_format": response_format,
            "model_override": model_override,
            "fallback_models_override": fallback_models_override,
            "on_stream_delta": on_stream_delta,
            "per_model_timeout_sec": per_model_timeout_sec,
            "per_model_timeout_overrides": per_model_timeout_overrides,
            "budget_tight_ratio": budget_tight_ratio,
            "truncation_max_count": truncation_max_count,
        }
        self.chat_kwargs = kw
        cb = on_stream_delta
        if cb is not None:
            for chunk in self._chunks:
                self.received_deltas.append(chunk)
                res = cb(chunk)
                if hasattr(res, "__await__"):
                    await res
        return SimpleNamespace(
            status=self._status,
            error_text=self._err,
            response_json=self._rj,
            response_text=self._rt,
            model="m1",
            tokens_prompt=10,
            tokens_completion=20,
            cost_usd=0.01,
            latency_ms=123,
        )

    async def chat_structured(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


async def _noop(_delta: str) -> None:
    return None


async def test_streaming_parses_response_json_and_builds_call_meta() -> None:
    llm = _FakeLLM(response_json={"summary_250": "S", "tldr": "T"})
    summary, call_meta, call_count = await summarize_streaming(
        llm_client=llm,
        messages=_MESSAGES,
        source_content="src",
        max_tokens=512,
        model_override=None,
        temperature=0.2,
        structured_output_mode=None,
        on_token=_noop,
    )
    assert summary["summary_250"] == "S"
    assert summary["tldr"] == "T"
    assert call_meta == {
        "model": "m1",
        "tokens_prompt": 10,
        "tokens_completion": 20,
        "cost_usd": 0.01,
        "latency_ms": 123,
    }
    # Requested streaming from the provider.
    assert llm.chat_kwargs.get("stream") is True
    assert call_count == 1


async def test_streaming_honors_json_schema_structured_output_mode() -> None:
    """audit #19: structured_output_mode='json_schema' constrains the provider.

    Previously the streaming path hardcoded response_format={'type':'json_object'}
    and silently ignored the configured mode.
    """
    llm = _FakeLLM(response_json={"summary_250": "S", "tldr": "T"})
    await summarize_streaming(
        llm_client=llm,
        messages=_MESSAGES,
        source_content="src",
        max_tokens=None,
        model_override=None,
        temperature=0.2,
        structured_output_mode="json_schema",
        on_token=_noop,
    )
    response_format = llm.chat_kwargs.get("response_format")
    assert isinstance(response_format, dict)
    assert response_format.get("type") == "json_schema"
    assert "json_schema" in response_format
    assert response_format["json_schema"].get("strict") is True


async def test_streaming_defaults_to_json_object_when_mode_unset() -> None:
    """With no structured_output_mode the provider gets plain json_object."""
    llm = _FakeLLM(response_json={"summary_250": "S"})
    await summarize_streaming(
        llm_client=llm,
        messages=_MESSAGES,
        source_content="src",
        max_tokens=None,
        model_override=None,
        temperature=0.2,
        structured_output_mode=None,
        on_token=_noop,
    )
    assert llm.chat_kwargs.get("response_format") == {"type": "json_object"}


async def test_streaming_falls_back_to_text_when_no_response_json() -> None:
    llm = _FakeLLM(response_text='{"summary_250": "from text"}')
    summary, _, _call_count = await summarize_streaming(
        llm_client=llm,
        messages=_MESSAGES,
        source_content="src",
        max_tokens=None,
        model_override=None,
        temperature=0.2,
        structured_output_mode=None,
        on_token=_noop,
    )
    assert summary["summary_250"] == "from text"


async def test_streaming_forwards_each_delta_to_on_token() -> None:
    seen: list[str] = []

    async def capture(delta: str) -> None:
        seen.append(delta)

    llm = _FakeLLM(
        response_json={"summary_250": "S"},
        chunks=['{"summary_250": ', '"S"}'],
    )
    await summarize_streaming(
        llm_client=llm,
        messages=_MESSAGES,
        source_content="src",
        max_tokens=None,
        model_override=None,
        temperature=0.2,
        structured_output_mode=None,
        on_token=capture,
    )
    assert seen == ['{"summary_250": ', '"S"}']


async def test_streaming_raises_on_non_ok_status() -> None:
    llm = _FakeLLM(status=CallStatus.ERROR, error_text="boom", response_json={"x": 1})
    with pytest.raises(ValueError, match="Streaming LLM call failed"):
        await summarize_streaming(
            llm_client=llm,
            messages=_MESSAGES,
            source_content="src",
            max_tokens=None,
            model_override=None,
            temperature=0.2,
            structured_output_mode=None,
            on_token=_noop,
        )


async def test_streaming_routes_unparseable_output_to_validation_repair() -> None:
    llm = _FakeLLM(response_text="not json at all")
    summary, call_meta, _call_count = await summarize_streaming(
        llm_client=llm,
        messages=_MESSAGES,
        source_content="src",
        max_tokens=None,
        model_override=None,
        temperature=0.2,
        structured_output_mode=None,
        on_token=_noop,
    )

    assert summary["__raw_stream_response__"] == "not json at all"
    assert summary["__stream_parse_error__"]
    assert call_meta["model"] == "m1"

    validation = await validate({"summary": summary}, deps=SimpleNamespace(graph_run_ledger=None))
    assert validation["validation_errors"]
