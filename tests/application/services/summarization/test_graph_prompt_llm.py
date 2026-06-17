"""T7: ported summarize logic (graph_prompt + graph_llm), CI-safe (no langgraph/DB).

Covers the application-layer modules the build_prompt / summarize / enrich nodes
delegate to: token budget, prompt assembly, long-context routing, the instructor
call with sticky-failure force-fallback, and two-pass enrichment.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.services.summarization import graph_llm, graph_prompt
from app.core.call_status import CallStatus
from app.core.summary_schema import SummaryModel


def _config(**over: Any) -> SummarizeConfig:
    base: dict[str, Any] = {
        "model": "base-model",
        "temperature": 0.2,
        "structured_output_mode": "json_schema",
        "long_context_threshold_tokens": 1_000_000,  # high -> no routing by default
    }
    base.update(over)
    return SummarizeConfig(**base)


def _structured(payload: dict[str, Any], *, model: str = "base-model") -> StructuredLLMResult:
    parsed = SummaryModel.model_construct(**payload)  # bypass validation for the canned result
    return StructuredLLMResult(
        parsed=parsed, tokens_prompt=10, tokens_completion=5, model_used=model
    )


# --------------------------------------------------------------------------- #
# graph_prompt
# --------------------------------------------------------------------------- #


def test_select_max_tokens_dynamic_and_clamped() -> None:
    # No configured ceiling -> dynamic budget, floored at 1536.
    assert graph_prompt.select_max_tokens("short", configured_max=None) == 1536
    # Configured ceiling clamps DOWN to the dynamic budget (still >= 1536 floor).
    assert graph_prompt.select_max_tokens("short", configured_max=4096) == 1536
    assert graph_prompt.select_max_tokens("short", configured_max=100) == 1536


def test_build_summary_user_prompt_structure() -> None:
    prompt = graph_prompt.build_summary_user_prompt(
        content_for_summary="Some article body.", chosen_lang="en"
    )
    assert "output ONLY a valid JSON object" in prompt
    assert "Respond in English." in prompt
    assert "SECURITY BOUNDARY" in prompt
    assert "prompt_injection_suspected=false" in prompt
    assert "<untrusted_source_content>\nSome article body.\n</untrusted_source_content>" in prompt


def test_build_summary_user_prompt_russian() -> None:
    prompt = graph_prompt.build_summary_user_prompt(content_for_summary="текст", chosen_lang="ru")
    assert "Respond in Russian." in prompt


def test_prepare_content_no_config_just_cleans() -> None:
    content, override = graph_prompt.prepare_content_for_summary("a\n\n\n\nb", config=None)
    assert override is None
    assert "\n\n\n" not in content  # clean_content_for_llm collapsed the whitespace


def test_prepare_content_routes_to_long_context_model() -> None:
    cfg = _config(long_context_threshold_tokens=1, long_context_model="long-ctx-model")
    _content, override = graph_prompt.prepare_content_for_summary("word " * 50, config=cfg)
    assert override == "long-ctx-model"  # over threshold + model present -> route, no truncation


def test_prepare_content_truncates_when_no_long_context_model() -> None:
    cfg = _config(long_context_threshold_tokens=1, long_context_model=None)
    long_text = "sentence one. " * 500
    content, override = graph_prompt.prepare_content_for_summary(long_text, config=cfg)
    assert override is None
    assert len(content) < len(long_text)  # truncated to the threshold budget


def test_load_instructor_system_prompt_en_and_ru() -> None:
    en = graph_prompt.load_instructor_system_prompt("en")
    ru = graph_prompt.load_instructor_system_prompt("ru")
    assert en.strip() and ru.strip()
    assert en != ru  # en/ru are distinct mirrored files


# --------------------------------------------------------------------------- #
# graph_llm
# --------------------------------------------------------------------------- #


def test_classify_sticky_error() -> None:
    assert (
        graph_llm.classify_sticky_error(ValueError("x per_model_timeout y")) == "per_model_timeout"
    )
    assert (
        graph_llm.classify_sticky_error(ValueError("repeated_truncation")) == "repeated_truncation"
    )
    assert graph_llm.classify_sticky_error(ValueError("nothing sticky")) is None


async def test_summarize_with_instructor_happy_path() -> None:
    llm = SimpleNamespace(
        chat_structured=AsyncMock(
            return_value=_structured(
                {"summary_250": "a", "summary_1000": "b", "tldr": "c"}, model="picked-model"
            )
        )
    )
    summary, meta = await graph_llm.summarize_with_instructor(
        llm_client=llm,
        messages=[{"role": "user", "content": "x"}],
        source_content="clean source",
        max_tokens=2048,
        model_override=None,
        temperature=0.2,
        max_retries=3,
        sticky_fallback_enabled=True,
        structured_output_mode="json_schema",
    )
    assert summary["summary_250"] == "a"
    assert summary["quality"]["prompt_injection_suspected"] is False
    assert meta["model"] == "picked-model"
    assert meta["tokens_prompt"] == 10


async def test_summarize_with_instructor_sticky_drops_override_and_retries() -> None:
    calls: list[dict[str, Any]] = []

    async def _chat_structured(messages: Any, **kwargs: Any) -> StructuredLLMResult:
        calls.append(kwargs)
        if kwargs.get("model_override") == "sticky-model":
            raise RuntimeError("per_model_timeout on sticky-model")
        return _structured({"summary_250": "a", "summary_1000": "b", "tldr": "c"})

    llm = SimpleNamespace(chat_structured=_chat_structured)
    summary, _meta = await graph_llm.summarize_with_instructor(
        llm_client=llm,
        messages=[{"role": "user", "content": "x"}],
        source_content="src",
        max_tokens=2048,
        model_override="sticky-model",
        temperature=0.2,
        max_retries=3,
        sticky_fallback_enabled=True,
        structured_output_mode="json_schema",
    )
    # First attempt uses the override (fails sticky), second drops it (None) and succeeds.
    assert calls[0]["model_override"] == "sticky-model"
    assert calls[1]["model_override"] is None
    assert summary["summary_250"] == "a"


async def test_summarize_with_instructor_raises_on_failure() -> None:
    llm = SimpleNamespace(chat_structured=AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(ValueError, match="Instructor LLM call failed"):
        await graph_llm.summarize_with_instructor(
            llm_client=llm,
            messages=[{"role": "user", "content": "x"}],
            source_content="src",
            max_tokens=2048,
            model_override=None,
            temperature=0.2,
            max_retries=3,
            sticky_fallback_enabled=True,
            structured_output_mode="json_schema",
        )


async def test_enrich_two_pass_merges_truthy_keys() -> None:
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                status=CallStatus.OK,
                response_text='{"seo_keywords": ["rust", "async"], "highlights": []}',
                error_text=None,
            )
        )
    )
    summary = {"summary_250": "core", "tldr": "t"}
    out, _call_meta = await graph_llm.enrich_two_pass(
        llm_client=llm,
        summary=summary,
        content_text="original content",
        chosen_lang="en",
        temperature=0.2,
        top_p=None,
        enrichment_max_tokens=4096,
    )
    assert out["seo_keywords"] == ["rust", "async"]
    assert "highlights" not in out  # empty list is falsy -> not merged


async def test_enrich_two_pass_returns_original_on_llm_error() -> None:
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(status=CallStatus.ERROR, response_text="", error_text="x")
        )
    )
    summary = {"summary_250": "core"}
    out, _call_meta = await graph_llm.enrich_two_pass(
        llm_client=llm,
        summary=summary,
        content_text="c",
        chosen_lang="en",
        temperature=0.2,
        top_p=None,
        enrichment_max_tokens=4096,
    )
    assert out == {"summary_250": "core"}  # unchanged
