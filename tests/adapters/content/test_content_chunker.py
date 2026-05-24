from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.content import content_chunker as module
from app.adapters.content.content_chunker import (
    ContentChunker,
    build_chunk_synthesis_user_content,
)
from app.core.call_status import CallStatus


class _AsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakeLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.responses.pop(0)


def _response(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        status=CallStatus.OK,
        error_text=None,
        response_json={"choices": [{"message": {"parsed": payload}}]},
        response_text="",
    )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            enable_chunking=True,
            chunk_max_chars=100,
            max_concurrent_calls=2,
        ),
        openrouter=SimpleNamespace(
            long_context_model="",
            model="small-model",
            temperature=0.2,
            top_p=0.9,
            max_tokens=1234,
            structured_output_mode="json_schema",
        ),
    )


def _chunker(llm: _FakeLLM | None = None) -> ContentChunker:
    return ContentChunker(
        _cfg(),
        llm or _FakeLLM([]),
        response_formatter=SimpleNamespace(),
        audit_func=lambda *_args, **_kwargs: None,
        sem=_AsyncContext,
    )


def test_content_chunker_builds_synthesis_prompt_and_estimates_thresholds() -> None:
    chunker = _chunker()

    prompt = build_chunk_synthesis_user_content(
        {
            "tldr": "Short draft",
            "summary_250": "Detailed draft",
            "key_ideas": ["A", "B"],
        },
        "ru",
    )

    assert "Respond in Russian." in prompt
    assert "Short draft" in prompt
    assert '"A"' in prompt
    assert chunker.estimate_max_chars_for_model(None, 500) == 500
    assert chunker.estimate_max_chars_for_model("gemini-3.1-pro", 500) == 3_000_000
    assert chunker.estimate_max_chars_for_model("small", 500) == 500


def test_content_chunker_decides_when_to_chunk() -> None:
    chunker = _chunker()

    should_chunk, max_chars, chunks = chunker.should_chunk_content(
        "Sentence one. Sentence two. Sentence three. " * 5,
        "en",
    )

    assert should_chunk is True
    assert max_chars == 80
    assert chunks is not None
    assert "".join(chunks)

    chunker.cfg.runtime.enable_chunking = False
    disabled, _, disabled_chunks = chunker.should_chunk_content("x" * 500, "en")
    assert disabled is False
    assert disabled_chunks is None


def test_content_chunker_parses_structured_and_text_responses() -> None:
    structured = _response({"summary_250": "from parsed"})
    assert ContentChunker._parse_llm_response_to_dict(structured) == {"summary_250": "from parsed"}

    text_response = SimpleNamespace(
        response_json=None, response_text='{"summary_250": "from text"}'
    )
    assert ContentChunker._parse_llm_response_to_dict(text_response) == {"summary_250": "from text"}

    invalid = SimpleNamespace(response_json=None, response_text="not json")
    assert ContentChunker._parse_llm_response_to_dict(invalid) is None


@pytest.mark.asyncio
async def test_content_chunker_processes_and_synthesizes_chunks(monkeypatch) -> None:
    llm = _FakeLLM(
        [
            _response(
                {
                    "summary_250": "Chunk one.",
                    "summary_1000": "Chunk one detailed.",
                    "tldr": "One.",
                    "key_ideas": ["A"],
                    "topic_tags": ["tag"],
                    "entities": {},
                    "estimated_reading_time_min": 1,
                }
            ),
            SimpleNamespace(
                status=CallStatus.ERROR,
                error_text="bad",
                response_json=None,
                response_text="",
            ),
            _response({"summary_250": "Synthesized.", "key_ideas": ["A"]}),
        ]
    )
    chunker = _chunker(llm)
    monkeypatch.setattr(module, "validate_and_shape_summary", lambda payload: payload)
    monkeypatch.setattr(
        chunker,
        "_build_structured_response_format",
        lambda: {"type": "json_object"},
    )

    result = await chunker.process_chunks(
        ["first chunk", "second chunk"],
        "system",
        "en",
        req_id=42,
        correlation_id="cid",
    )

    assert result == {"summary_250": "Synthesized.", "key_ideas": ["A"]}
    assert len(llm.calls) == 3
    assert llm.calls[0]["kwargs"]["request_id"] == 42
    assert llm.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}
