"""The summarize node routes on ``state['stream']`` (ADR-0017): the streaming
runner gets the chat(stream=True) token path; ainvoke keeps the structured path
(byte-identical to T7/legacy, so T9 parity is untouched)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.application.graphs.summarize.nodes.summarize import summarize
from app.core.call_status import CallStatus

_MESSAGES = [{"role": "user", "content": "x"}]


class _FakeLLM:
    def __init__(self) -> None:
        self.chat_called = False
        self.chat_structured_called = False

    async def chat(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
        self.chat_called = True
        cb = kw.get("on_stream_delta")
        if cb is not None:
            res = cb('{"summary_250": "S", "tldr": "T"}')
            if hasattr(res, "__await__"):
                await res
        return SimpleNamespace(
            status=CallStatus.OK,
            error_text=None,
            response_json={"summary_250": "S", "tldr": "T"},
            response_text="",
            model="m-stream",
            tokens_prompt=1,
            tokens_completion=2,
            cost_usd=0.0,
            latency_ms=5,
        )

    async def chat_structured(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
        self.chat_structured_called = True
        parsed = SimpleNamespace(model_dump=lambda: {"summary_250": "struct"})
        return SimpleNamespace(
            parsed=parsed,
            model_used="m-struct",
            tokens_prompt=1,
            tokens_completion=2,
            cost_usd=0.0,
            latency_ms=5,
        )


def _deps(llm: _FakeLLM) -> Any:
    return SimpleNamespace(llm_client=llm, config=None)


async def test_stream_flag_routes_to_streaming_path() -> None:
    llm = _FakeLLM()
    state = {
        "stream": True,
        "messages": _MESSAGES,
        "content_for_summary": "src",
        "request_id": 7,
        "correlation_id": "c",
        "call_count": 0,
    }
    out = await summarize(state, deps=_deps(llm))

    assert llm.chat_called is True
    assert llm.chat_structured_called is False
    assert out["summary"]["summary_250"] == "S"
    assert out["call_count"] == 1
    # streaming path used a raw chat call, not instructor structured output.
    assert out["llm_calls"][0]["structured_output_used"] is False
    assert out["llm_calls"][0]["attempt_trigger"] == "graph_node"


async def test_no_stream_flag_keeps_structured_path() -> None:
    llm = _FakeLLM()
    state = {
        "messages": _MESSAGES,
        "content_for_summary": "src",
        "request_id": 7,
        "correlation_id": "c",
        "call_count": 0,
    }
    out = await summarize(state, deps=_deps(llm))

    assert llm.chat_structured_called is True
    assert llm.chat_called is False
    assert out["summary"]["summary_250"] == "struct"
    assert out["llm_calls"][0]["structured_output_used"] is True


async def test_no_messages_is_a_noop_either_mode() -> None:
    llm = _FakeLLM()
    out = await summarize({"stream": True, "messages": []}, deps=_deps(llm))
    assert out == {}
    assert llm.chat_called is False
