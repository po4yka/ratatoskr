"""T7: summarize / validate / repair / enrich node bodies (CI-safe, no langgraph/DB)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.application.graphs.summarize.deps import SummarizeConfig, SummarizeDeps
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.nodes import enrich, repair, summarize, validate
from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS
from app.core.call_status import CallStatus
from app.core.summary_schema import SummaryModel

_VALID = {"summary_250": "a summary", "summary_1000": "a longer summary", "tldr": "tl;dr"}


def _structured(payload: dict[str, Any], *, model: str = "m") -> StructuredLLMResult:
    return StructuredLLMResult(
        parsed=SummaryModel.model_construct(**payload),
        tokens_prompt=10,
        tokens_completion=5,
        model_used=model,
    )


def _config(**over: Any) -> SummarizeConfig:
    base: dict[str, Any] = {
        "model": "base-model",
        "llm_provider": "openrouter",
        "temperature": 0.2,
        "structured_output_mode": "json_schema",
        "long_context_threshold_tokens": 1_000_000,
    }
    base.update(over)
    return SummarizeConfig(**base)


def _deps(*, llm_client: Any = None, config: SummarizeConfig | None = None) -> SummarizeDeps:
    m = MagicMock()
    return SummarizeDeps(
        llm_client=llm_client or m,
        retrieval=m,
        extraction=m,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
        config=config,
    )


def _prompted_state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": "cid-1",
        "request_id": 1,
        "lang": "en",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
        "content_for_summary": "the source",
        "call_count": 0,
        "repair_attempts": 0,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# summarize node
# --------------------------------------------------------------------------- #


async def test_summarize_node_produces_summary_and_graph_node_llm_call() -> None:
    llm = SimpleNamespace(chat_structured=AsyncMock(return_value=_structured(_VALID)))
    out = await summarize(_prompted_state(), deps=_deps(llm_client=llm, config=_config()))
    assert out["summary"]["summary_250"] == "a summary"
    assert out["call_count"] == 1
    assert len(out["llm_calls"]) == 1
    rec = out["llm_calls"][0]
    assert rec["attempt_trigger"] == "graph_node"
    assert rec["request_id"] == 1
    assert rec["provider"] == "openrouter"
    assert rec["status"] == "ok"


async def test_summarize_node_noop_without_messages() -> None:
    out = await summarize({"request_id": 1}, deps=_deps(llm_client=MagicMock(), config=_config()))
    assert out == {}


async def test_summarize_node_failure_propagates() -> None:
    llm = SimpleNamespace(chat_structured=AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(ValueError, match="Instructor LLM call failed"):
        await summarize(_prompted_state(), deps=_deps(llm_client=llm, config=_config()))


# --------------------------------------------------------------------------- #
# validate node
# --------------------------------------------------------------------------- #


async def test_validate_node_shapes_valid_summary() -> None:
    out = await validate({"summary": dict(_VALID)}, deps=MagicMock())
    assert out["validation_errors"] == []
    assert out["summary"]["summary_250"]  # canonical shaped payload present


async def test_validate_node_merges_source_coverage_from_extraction_state() -> None:
    out = await validate(
        {
            "summary": dict(_VALID),
            "content_source": "academic_paper_abstract_only",
            "content_for_summary": "Only the abstract was available.",
        },
        deps=MagicMock(),
    )

    assert out["validation_errors"] == []
    assert out["summary"]["summary_quality"]["source_coverage"] == "abstract_only"


async def test_validate_node_marks_repaired_summary_quality_after_success() -> None:
    out = await validate(
        {
            "summary": dict(_VALID),
            "repair_attempts": 1,
        },
        deps=MagicMock(),
    )

    quality = out["summary"]["summary_quality"]
    assert quality["repair_attempted"] is True
    assert quality["repair_succeeded"] is True


async def test_validate_node_flags_invalid_summary() -> None:
    out = await validate({"summary": {"unrelated": "x"}}, deps=MagicMock())
    assert out["validation_errors"]  # non-empty -> routes to repair


async def test_validate_node_empty_summary_is_valid_empty() -> None:
    out = await validate({}, deps=MagicMock())
    assert out == {"validation_errors": []}


# --------------------------------------------------------------------------- #
# repair node
# --------------------------------------------------------------------------- #


async def test_repair_node_budget_exhaustion_raises() -> None:
    with pytest.raises(CallBudgetExceeded):
        await repair(
            {"repair_attempts": MAX_REPAIR_ATTEMPTS},
            deps=_deps(llm_client=MagicMock(), config=_config()),
        )


async def test_repair_node_reruns_and_records_call() -> None:
    llm = SimpleNamespace(chat_structured=AsyncMock(return_value=_structured(_VALID)))
    out = await repair(_prompted_state(), deps=_deps(llm_client=llm, config=_config()))
    assert out["repair_attempts"] == 1
    assert out["summary"]["summary_250"] == "a summary"
    assert out["llm_calls"][0]["attempt_trigger"] == "graph_node"


async def test_repair_node_swallows_llm_failure_and_advances_budget() -> None:
    llm = SimpleNamespace(chat_structured=AsyncMock(side_effect=RuntimeError("boom")))
    out = await repair(_prompted_state(), deps=_deps(llm_client=llm, config=_config()))
    assert out == {"repair_attempts": 1}  # no summary/llm_calls; budget advanced


async def test_repair_node_without_messages_only_advances_budget() -> None:
    out = await repair({"repair_attempts": 0}, deps=_deps(llm_client=MagicMock(), config=_config()))
    assert out == {"repair_attempts": 1}


# --------------------------------------------------------------------------- #
# enrich node
# --------------------------------------------------------------------------- #


async def test_enrich_node_disabled_is_noop() -> None:
    out = await enrich(
        {"summary": dict(_VALID), "two_pass_eligible": True},
        deps=_deps(llm_client=MagicMock(), config=_config(two_pass_enabled=False)),
    )
    assert out == {}


async def test_enrich_node_noop_without_config() -> None:
    out = await enrich(
        {"summary": dict(_VALID), "two_pass_eligible": True},
        deps=_deps(llm_client=MagicMock(), config=None),
    )
    assert out == {}


def _enriching_llm() -> Any:
    return SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                status=CallStatus.OK,
                response_text='{"seo_keywords": ["x", "y"]}',
                error_text=None,
            )
        )
    )


async def test_enrich_node_enabled_merges_keys() -> None:
    llm = _enriching_llm()
    out = await enrich(
        {
            "summary": {"summary_250": "s"},
            "content_for_summary": "c",
            "lang": "en",
            "two_pass_eligible": True,
        },
        deps=_deps(llm_client=llm, config=_config(two_pass_enabled=True)),
    )
    assert out["summary"]["seo_keywords"] == ["x", "y"]


async def test_enrich_node_noop_when_not_eligible_even_if_enabled() -> None:
    """audit #20: the content-only path leaves two_pass_eligible False, so the
    enrich node must no-op even when config.two_pass_enabled is True (no LLM call).
    """
    llm = _enriching_llm()
    out = await enrich(
        # No ``two_pass_eligible`` key -> the content-only ``summarize`` entrypoint.
        {"summary": {"summary_250": "s"}, "content_for_summary": "c", "lang": "en"},
        deps=_deps(llm_client=llm, config=_config(two_pass_enabled=True)),
    )
    assert out == {}
    llm.chat.assert_not_awaited()


async def test_enrich_node_noop_when_eligible_false_explicit() -> None:
    """An explicit two_pass_eligible=False also short-circuits enrichment."""
    llm = _enriching_llm()
    out = await enrich(
        {
            "summary": {"summary_250": "s"},
            "content_for_summary": "c",
            "lang": "en",
            "two_pass_eligible": False,
        },
        deps=_deps(llm_client=llm, config=_config(two_pass_enabled=True)),
    )
    assert out == {}
    llm.chat.assert_not_awaited()
