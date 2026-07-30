"""Tests for StarListClassifierService — the constrained LLM pick."""

from __future__ import annotations

import json

import pytest

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.application.services.star_list_classifier import StarListClassifierService
from app.core.star_list_suggestion_schema import StarListCandidate

_LISTS = [
    StarListCandidate(name="Android", description="Android app and library work"),
    StarListCandidate(name="Kubernetes"),
]


class _FakeLLM:
    provider_name = "fake"

    def __init__(self, response_text: str | None = None, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._response_text = response_text
        self._error = error

    async def chat_structured(self, messages, *, response_model, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self._error is not None:
            raise self._error
        return StructuredLLMResult(
            parsed=response_model(**json.loads(self._response_text or "{}")),
            cost_usd=0.001,
            latency_ms=42,
            model_used="test-model",
        )

    async def aclose(self) -> None:
        return None


async def _classify(llm: _FakeLLM, *, candidates: list[StarListCandidate] | None = None):
    service = StarListClassifierService(llm_client=llm)
    return await service.classify(
        candidates=_LISTS if candidates is None else candidates,
        full_name="square/metro",
        description="An Android build tool",
        language="Kotlin",
        topics=["android", "gradle"],
        readme_excerpt="Builds apps.",
    )


@pytest.mark.asyncio
async def test_a_valid_pick_is_returned():
    llm = _FakeLLM('{"list_name": "Android", "confidence": 0.9, "reason": "android tooling"}')

    choice = await _classify(llm)

    assert choice is not None
    assert choice.list_name == "Android"
    assert choice.confidence == 0.9


@pytest.mark.asyncio
async def test_a_hallucinated_list_is_rejected():
    """Writing a name that does not exist would clear membership, not set it."""
    llm = _FakeLLM('{"list_name": "Mobile Stuff", "confidence": 0.95, "reason": "made up"}')

    assert await _classify(llm) is None


@pytest.mark.asyncio
async def test_an_empty_pick_is_passed_through_as_a_real_answer():
    llm = _FakeLLM('{"list_name": "", "confidence": 0.8, "reason": "nothing fits"}')

    choice = await _classify(llm)

    assert choice is not None
    assert choice.list_name == ""


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_tolerated():
    llm = _FakeLLM('{"list_name": "  Android  ", "confidence": 0.7, "reason": "ok"}')

    choice = await _classify(llm)

    assert choice is not None
    assert choice.list_name == "Android"


@pytest.mark.asyncio
async def test_an_llm_failure_yields_no_pick():
    llm = _FakeLLM(error=RuntimeError("provider down"))

    assert await _classify(llm) is None


@pytest.mark.asyncio
async def test_no_candidates_means_no_call_at_all():
    llm = _FakeLLM('{"list_name": "Android", "confidence": 0.9, "reason": "x"}')

    assert await _classify(llm, candidates=[]) is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_the_prompt_carries_every_live_list_name():
    llm = _FakeLLM('{"list_name": "Android", "confidence": 0.9, "reason": "x"}')

    await _classify(llm)

    prompt = llm.calls[0]["messages"][0]["content"]
    assert "Android — Android app and library work" in prompt
    # A list without a description must not render a dangling separator.
    assert "Kubernetes\n" in prompt or prompt.rstrip().endswith("Kubernetes")
    assert "Kubernetes —" not in prompt


@pytest.mark.asyncio
async def test_both_language_prompts_render():
    """Operating Rule 7: the en/ru prompt pair must stay usable together."""
    for lang in ("en", "ru"):
        llm = _FakeLLM('{"list_name": "Android", "confidence": 0.9, "reason": "x"}')
        service = StarListClassifierService(llm_client=llm, lang=lang)
        choice = await service.classify(
            candidates=_LISTS,
            full_name="square/metro",
            description="An Android build tool",
            language="Kotlin",
            topics=["android"],
            readme_excerpt="Builds apps.",
        )
        assert choice is not None
        assert "Android" in llm.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_every_llm_call_is_persisted_even_on_failure():
    """Operating Rule 3: failures go to llm_calls too."""
    from unittest.mock import AsyncMock, MagicMock

    llm_repo = MagicMock()
    llm_repo.async_insert_llm_call = AsyncMock()
    service = StarListClassifierService(
        llm_client=_FakeLLM(error=RuntimeError("boom")),
        llm_repo=llm_repo,
    )

    await service.classify(
        candidates=_LISTS,
        full_name="square/metro",
        description=None,
        language=None,
        topics=[],
        readme_excerpt=None,
    )

    llm_repo.async_insert_llm_call.assert_awaited_once()
    payload = llm_repo.async_insert_llm_call.await_args.args[0]
    assert payload["status"] == "error"
    assert payload["endpoint"] == "star_list_classifier"
    assert payload["request_id"] is None
