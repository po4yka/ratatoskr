"""Tests for GAP 1-5 behavioral-parity closure in the summarize graph.

Each section corresponds to one gap. All tests are CI-safe (no langgraph / DB).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapter_models.llm.llm_models import StructuredLLMResult
from app.application.graphs.summarize.deps import SummarizeConfig, SummarizeDeps
from app.application.graphs.summarize.lifecycle import route_terminal_failure
from app.application.graphs.summarize.nodes import (
    build_prompt,
    enrich,
    notify,
    persist,
    summarize,
)
from app.application.graphs.summarize.state import SummarizeState
from app.application.ports.summaries import SummaryFinalizeResult
from app.application.services.summarization.metadata_backfill import (
    _extract_heading_title,
    _flatten_crawl_metadata,
    backfill_summary_metadata,
)
from app.core.call_status import CallStatus
from app.core.content_classifier import ContentTier
from app.core.summary_schema import SummaryModel

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID = {"summary_250": "a summary", "summary_1000": "a longer summary", "tldr": "tl;dr"}


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


def _deps(**over: Any) -> SummarizeDeps:
    m = MagicMock()
    m.async_update_request_status = AsyncMock()
    defaults: dict[str, Any] = {
        "llm_client": m,
        "retrieval": m,
        "extraction": m,
        "stream_sink": m,
        "summaries": m,
        "requests": m,
        "summary_index": m,
    }
    defaults.update(over)
    return SummarizeDeps(**defaults)


def _structured(payload: dict[str, Any] = _VALID, *, model: str = "m") -> StructuredLLMResult:
    return StructuredLLMResult(
        parsed=SummaryModel.model_construct(**payload),
        tokens_prompt=10,
        tokens_completion=5,
        model_used=model,
    )


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": "cid-gap",
        "request_id": 42,
        "lang": "en",
        "grounding_ids": [],
        "summary": {},
        "validation_errors": [],
        "repair_attempts": 0,
        "call_count": 0,
        "llm_calls": [],
        # URL-flow default: the enrich node's two-pass pass is eligible (audit
        # #20). Content-only callers would leave this False.
        "two_pass_eligible": True,
    }
    base.update(over)
    return base


# ===========================================================================
# GAP 1 — content-aware tier routing in build_prompt
# ===========================================================================


async def test_gap1_tier_routing_calls_model_router_when_enabled() -> None:
    """model_router is called with (tier, content_length) when routing_enabled=True."""
    router = MagicMock(return_value="tier-model-x")
    deps = _deps(
        config=_config(routing_enabled=True),
        model_router=router,
    )
    state = _state(source_text="some content about kubernetes microservices")
    out = await build_prompt(state, deps=deps)
    assert out["model_override"] == "tier-model-x"
    router.assert_called_once()
    tier_arg, length_arg = router.call_args.args
    assert isinstance(tier_arg, ContentTier)
    assert isinstance(length_arg, int) and length_arg > 0


async def test_gap1_tier_routing_skipped_when_routing_disabled() -> None:
    """model_router is NOT called when routing_enabled=False."""
    router = MagicMock(return_value="should-not-use")
    deps = _deps(
        config=_config(routing_enabled=False),
        model_router=router,
    )
    state = _state(source_text="article text here")
    out = await build_prompt(state, deps=deps)
    router.assert_not_called()
    assert out["model_override"] == ""


async def test_gap1_tier_routing_skipped_when_long_context_override_set() -> None:
    """Long-context override takes priority -- tier routing is NOT called."""
    router = MagicMock(return_value="should-not-use")
    # threshold=1 -> any content triggers long-context; model_override is set before routing
    deps = _deps(
        config=_config(
            routing_enabled=True,
            long_context_threshold_tokens=1,
            long_context_model="long-ctx-model",
        ),
        model_router=router,
    )
    state = _state(source_text="enough content to exceed 1-token threshold")
    out = await build_prompt(state, deps=deps)
    router.assert_not_called()
    assert out["model_override"] == "long-ctx-model"


async def test_gap1_tier_routing_skipped_when_model_router_is_none() -> None:
    """routing_enabled=True but no model_router -> conservative path (no override)."""
    deps = _deps(config=_config(routing_enabled=True), model_router=None)
    state = _state(source_text="article text here")
    out = await build_prompt(state, deps=deps)
    assert out["model_override"] == ""


# ===========================================================================
# Article-vision routing in build_prompt (audit #2)
# ===========================================================================

_IMG = [
    "https://cdn.example.com/photos/a.jpg",
    "https://cdn.example.com/photos/b.jpg",
    "https://cdn.example.com/photos/c.jpg",
]


def _vision_config(**over: Any) -> SummarizeConfig:
    base: dict[str, Any] = {
        "article_vision_enabled": True,
        "article_vision_min_images": 3,
        "vision_model": "vision-x",
    }
    base.update(over)
    return _config(**base)


async def test_vision_routes_to_vision_model_with_multimodal_message() -> None:
    """>=min_images valid images -> vision model override + multimodal user message."""
    deps = _deps(config=_vision_config())
    state = _state(source_text="image-rich article body", images=_IMG)
    out = await build_prompt(state, deps=deps)

    assert out["model_override"] == "vision-x"
    user_msg = out["messages"][1]
    assert user_msg["role"] == "user"
    # Multimodal content: leading text part + one image_url part per valid image.
    parts = user_msg["content"]
    assert isinstance(parts, list)
    assert parts[0]["type"] == "text"
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert [p["image_url"]["url"] for p in image_parts] == _IMG


async def test_vision_skipped_below_min_images_threshold() -> None:
    """Fewer than min_images valid images -> text-only path, no vision override."""
    deps = _deps(config=_vision_config())  # min_images=3
    state = _state(source_text="body", images=_IMG[:2])
    out = await build_prompt(state, deps=deps)

    assert out["model_override"] == ""
    assert isinstance(out["messages"][1]["content"], str)  # plain text message


async def test_vision_skipped_when_disabled() -> None:
    """article_vision_enabled=False -> text-only even with enough images."""
    deps = _deps(config=_vision_config(article_vision_enabled=False))
    state = _state(source_text="body", images=_IMG)
    out = await build_prompt(state, deps=deps)

    assert out["model_override"] == ""
    assert isinstance(out["messages"][1]["content"], str)


async def test_vision_filters_invalid_image_urls_before_thresholding() -> None:
    """Invalid URLs (non-https / template literals / non-image ext) are dropped.

    Three candidates but only two pass the validator -> below the 3-image threshold,
    so the text path is taken. Pins that the SAME filter governs model selection and
    message assembly (no divergence with the legacy path)."""
    deps = _deps(config=_vision_config())
    images = [
        "https://cdn.example.com/photos/a.jpg",
        "http://insecure.example.com/b.jpg",  # not https -> dropped
        "https://cdn.example.com/image/fetch/$s_!template!",  # leaked literal -> dropped
    ]
    state = _state(source_text="body", images=images)
    out = await build_prompt(state, deps=deps)

    assert out["model_override"] == ""  # only 1 valid < 3
    assert isinstance(out["messages"][1]["content"], str)


async def test_vision_override_yields_to_long_context() -> None:
    """Long-context override wins over vision (legacy _prepare_summary_content order)."""
    deps = _deps(
        config=_vision_config(
            long_context_threshold_tokens=1,
            long_context_model="long-ctx-model",
        )
    )
    state = _state(source_text="enough body to exceed the 1-token threshold", images=_IMG)
    out = await build_prompt(state, deps=deps)

    # Long-context pins the model, but the message is still multimodal (vision active).
    assert out["model_override"] == "long-ctx-model"
    assert isinstance(out["messages"][1]["content"], list)


# ===========================================================================
# GAP 2 — Redis LLM summary cache
# ===========================================================================


async def test_gap2_cache_hit_returns_cached_summary_without_llm_call() -> None:
    """Cache hit: returns cached summary, no LLM call, empty llm_calls list."""
    cached = dict(_VALID)
    cache = SimpleNamespace(
        get=AsyncMock(return_value=cached),
        set=AsyncMock(),
    )
    llm = SimpleNamespace(chat_structured=AsyncMock(side_effect=AssertionError("must not call")))
    deps = _deps(
        llm_client=llm,
        config=_config(),
        summary_cache=cache,
    )
    state = _state(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        content_for_summary="the source",
        dedupe_hash="abc123",
        lang="en",
    )
    out = await summarize(state, deps=deps)
    assert out["summary"] == cached
    assert out["llm_calls"] == []
    cache.get.assert_awaited_once_with("abc123", "en")
    cache.set.assert_not_awaited()


async def test_gap2_cache_miss_calls_llm_without_writing_cache_from_summarize() -> None:
    """Cache-poisoning fix: on a miss, summarize() calls the LLM but never writes
    the cache itself -- the write moved to ``persist``, which only runs after
    ``validate`` confirms the summary against the contract (see
    ``test_gap2_persist_writes_validated_summary_to_cache`` below)."""
    cache = SimpleNamespace(
        get=AsyncMock(return_value=None),
        set=AsyncMock(),
    )
    llm = SimpleNamespace(chat_structured=AsyncMock(return_value=_structured()))
    deps = _deps(
        llm_client=llm,
        config=_config(),
        summary_cache=cache,
    )
    state = _state(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        content_for_summary="the source",
        dedupe_hash="abc123",
        lang="en",
    )
    out = await summarize(state, deps=deps)
    assert out["summary"]["summary_250"] == "a summary"
    cache.get.assert_awaited_once_with("abc123", "en")
    cache.set.assert_not_awaited()


async def test_gap2_persist_writes_validated_summary_to_cache() -> None:
    """persist() writes the validated summary to cache once validate has
    succeeded (no validation_errors on state, non-streaming)."""
    cache = SimpleNamespace(get=AsyncMock(), set=AsyncMock())
    deps = _deps(summary_cache=cache)
    state = _state(
        request_id=None,
        summary=dict(_VALID),
        dedupe_hash="abc123",
        lang="en",
        validation_errors=[],
    )
    await persist(state, deps=deps)
    cache.set.assert_awaited_once_with("abc123", "en", dict(_VALID))


async def test_gap2_persist_does_not_cache_summary_with_validation_errors() -> None:
    """Cache-poisoning fix: a summary still carrying validation_errors is never
    cached (defense-in-depth; the graph topology already prevents this state
    from reaching persist, since a failed validate routes to repair, not
    enrich -> persist)."""
    cache = SimpleNamespace(get=AsyncMock(), set=AsyncMock())
    deps = _deps(summary_cache=cache)
    state = _state(
        request_id=None,
        summary=dict(_VALID),
        dedupe_hash="abc123",
        lang="en",
        validation_errors=["missing tldr"],
    )
    await persist(state, deps=deps)
    cache.set.assert_not_awaited()


async def test_gap2_persist_skips_cache_write_when_streaming() -> None:
    """Streaming path never writes the cache (live-UX path, ADR-0017)."""
    cache = SimpleNamespace(get=AsyncMock(), set=AsyncMock())
    deps = _deps(summary_cache=cache)
    state = _state(
        request_id=None,
        summary=dict(_VALID),
        dedupe_hash="abc123",
        lang="en",
        validation_errors=[],
        stream=True,
    )
    await persist(state, deps=deps)
    cache.set.assert_not_awaited()


async def test_gap2_persist_skips_cache_write_when_dedupe_hash_missing() -> None:
    """No cache write when dedupe_hash is absent from state."""
    cache = SimpleNamespace(get=AsyncMock(), set=AsyncMock())
    deps = _deps(summary_cache=cache)
    state = _state(
        request_id=None,
        summary=dict(_VALID),
        lang="en",
        validation_errors=[],
    )
    await persist(state, deps=deps)
    cache.set.assert_not_awaited()


async def test_gap2_persist_skips_cache_write_when_summary_cache_is_none() -> None:
    """No cache interaction when deps.summary_cache is None."""
    deps = _deps(summary_cache=None)
    state = _state(
        request_id=None,
        summary=dict(_VALID),
        dedupe_hash="abc123",
        lang="en",
        validation_errors=[],
    )
    # Must not raise even though there is no cache to write to.
    await persist(state, deps=deps)


async def test_gap2_no_cache_when_summary_cache_is_none() -> None:
    """No cache interaction when deps.summary_cache is None."""
    llm = SimpleNamespace(chat_structured=AsyncMock(return_value=_structured()))
    deps = _deps(llm_client=llm, config=_config(), summary_cache=None)
    state = _state(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        content_for_summary="the source",
        dedupe_hash="abc123",
    )
    out = await summarize(state, deps=deps)
    assert out["summary"]["summary_250"] == "a summary"


async def test_gap2_no_cache_when_dedupe_hash_missing() -> None:
    """No cache lookup when dedupe_hash is absent from state."""
    cache = SimpleNamespace(get=AsyncMock(), set=AsyncMock())
    llm = SimpleNamespace(chat_structured=AsyncMock(return_value=_structured()))
    deps = _deps(llm_client=llm, config=_config(), summary_cache=cache)
    state = _state(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        content_for_summary="the source",
        # no dedupe_hash
    )
    out = await summarize(state, deps=deps)
    cache.get.assert_not_awaited()


# ===========================================================================
# GAP 3a — failure llm_calls persistence (summarize node)
# ===========================================================================


async def test_gap3a_summarize_failure_tags_exception_with_llm_failure_record() -> None:
    """On LLM failure, exc.llm_failure_records is set before re-raise."""
    llm = SimpleNamespace(chat_structured=AsyncMock(side_effect=RuntimeError("boom")))
    deps = _deps(llm_client=llm, config=_config())
    state = _state(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        content_for_summary="source",
    )
    with pytest.raises(ValueError) as exc_info:
        await summarize(state, deps=deps)
    # _tag_failure puts records on the original exc; ValueError wraps it via `from exc`
    # so check both the value and its __cause__
    inner = exc_info.value.__cause__ or exc_info.value
    assert hasattr(inner, "llm_failure_records"), "llm_failure_records must be attached to exc"
    rec = inner.llm_failure_records[0]
    assert rec["status"] == "error"
    assert rec["attempt_trigger"] == "graph_node"
    assert rec["request_id"] == 42
    # FIX-3: model must fall back to config model (never None per rule 11)
    assert rec["model"] == "base-model", f"expected config model, got {rec['model']!r}"
    # FIX-3: error_text must be a string (not None)
    assert isinstance(rec.get("error_text"), str) and rec["error_text"]


async def test_gap3a_failure_record_uses_real_llm_result_when_available() -> None:
    """FIX-3: when __llm_result__ is on the exc, use its model/error_text/latency."""
    raw_result = SimpleNamespace(
        model="real-provider-model",
        model_used=None,
        error_text="provider rate-limited",
        tokens_prompt=7,
        tokens_completion=0,
        cost_usd=None,
        latency_ms=1234,
    )
    inner_exc = RuntimeError("boom")
    inner_exc.__llm_result__ = raw_result  # type: ignore[attr-defined]

    llm = SimpleNamespace(chat_structured=AsyncMock(side_effect=inner_exc))
    deps = _deps(llm_client=llm, config=_config())
    state = _state(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        content_for_summary="source",
    )
    with pytest.raises(ValueError) as exc_info:
        await summarize(state, deps=deps)
    inner = exc_info.value.__cause__ or exc_info.value
    assert hasattr(inner, "llm_failure_records")
    rec = inner.llm_failure_records[0]
    assert rec["model"] == "real-provider-model"
    assert rec["error_text"] == "provider rate-limited"
    assert rec["latency_ms"] == 1234
    assert rec["tokens_prompt"] == 7


async def test_gap3a_route_terminal_failure_drains_llm_failure_records() -> None:
    """route_terminal_failure writes llm_failure_records to llm_repo."""
    llm_repo = SimpleNamespace(async_insert_llm_call=AsyncMock(return_value=1))
    requests_mock = SimpleNamespace(
        async_update_request_error=AsyncMock(),
        async_get_request_by_id=AsyncMock(return_value=None),
    )
    summaries_mock = MagicMock()
    deps = _deps(
        llm_repo=llm_repo,
        requests=requests_mock,
        summaries=summaries_mock,
    )

    exc = ValueError("LLM call failed")
    exc.__dict__["llm_failure_records"] = [
        {
            "request_id": 42,
            "provider": "openrouter",
            "model": None,
            "status": "error",
            "attempt_trigger": "graph_node",
            "error_text": "boom",
        }
    ]

    with patch(
        "app.observability.failure_observability.persist_request_failure",
        new=AsyncMock(),
    ):
        await route_terminal_failure(cast("SummarizeState", _state()), deps, exc)

    llm_repo.async_insert_llm_call.assert_awaited_once()


# ===========================================================================
# GAP 3b — enrich_two_pass records enrichment LLM call
# ===========================================================================


async def test_gap3b_enrich_node_records_llm_call_in_llm_calls() -> None:
    """enrich node with two_pass_enabled=True writes a llm_calls record."""
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                status=CallStatus.OK,
                response_text='{"seo_keywords": ["x"]}',
                model="enrich-model",
                tokens_prompt=5,
                tokens_completion=3,
                cost_usd=None,
                latency_ms=100,
                error_text=None,
            )
        )
    )
    deps = _deps(llm_client=llm, config=_config(two_pass_enabled=True))
    state = _state(
        summary={"summary_250": "s"},
        content_for_summary="content",
        lang="en",
    )
    out = await enrich(state, deps=deps)
    assert "llm_calls" in out
    assert len(out["llm_calls"]) == 1
    rec = out["llm_calls"][0]
    assert rec["attempt_trigger"] == "graph_node"
    assert rec["model"] == "enrich-model"
    assert rec["request_id"] == 42


async def test_gap3b_enrich_node_persists_failed_attempt_on_exception() -> None:
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("enrich boom")))
    deps = _deps(llm_client=llm, config=_config(two_pass_enabled=True))
    state = _state(summary={"summary_250": "s"}, content_for_summary="c", lang="en")
    out = await enrich(state, deps=deps)
    assert len(out["llm_calls"]) == 1
    assert out["llm_calls"][0]["status"] == "error"
    assert out["llm_calls"][0]["error_text"] == "enrich boom"


async def test_fix5_enrich_node_persists_provider_failure_status() -> None:
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                status=CallStatus.ERROR,
                response_text="",
                model="enrich-model",
                tokens_prompt=2,
                tokens_completion=0,
                cost_usd=None,
                latency_ms=50,
                error_text="provider error",
            )
        )
    )
    deps = _deps(llm_client=llm, config=_config(two_pass_enabled=True))
    state = _state(summary={"summary_250": "s"}, content_for_summary="c", lang="en")
    out = await enrich(state, deps=deps)
    assert len(out["llm_calls"]) == 1
    assert out["llm_calls"][0]["status"] == "error"
    assert out["llm_calls"][0]["error_text"] == "provider error"


# ===========================================================================
# GAP 4 — metadata backfill in persist
# ===========================================================================


async def test_gap4_persist_backfills_canonical_url_from_request() -> None:
    """persist calls backfill_summary_metadata which fills canonical_url."""
    summary: dict[str, Any] = {
        "summary_250": "s",
        "summary_1000": "l",
        "tldr": "t",
        "metadata": {},
    }
    crawl_repo = SimpleNamespace(async_get_crawl_result_by_request=AsyncMock(return_value=None))
    request_repo = SimpleNamespace(
        async_get_request_by_id=AsyncMock(
            return_value={"normalized_url": "https://example.com/article"}
        ),
        async_update_request_status=AsyncMock(),
    )
    summary_repo = SimpleNamespace(
        async_persist_summary_with_llm_calls=AsyncMock(
            return_value=SummaryFinalizeResult(summary_id=99, version=1)
        ),
    )
    summary_index = SimpleNamespace(index_summary=AsyncMock())
    deps = _deps(
        crawl_repo=crawl_repo,
        requests=request_repo,
        summaries=summary_repo,
        summary_index=summary_index,
        llm_repo=None,
    )
    state = _state(
        summary=summary,
        request_id=42,
        lang="en",
        source_text="article text",
    )
    out = await persist(state, deps=deps)
    # The atomically persisted summary should have canonical_url.
    call_kwargs = summary_repo.async_persist_summary_with_llm_calls.call_args.kwargs
    saved = call_kwargs["json_payload"]
    assert saved.get("metadata", {}).get("canonical_url") == "https://example.com/article"


async def test_gap4_persist_backfills_domain_from_url() -> None:
    """domain is derived from canonical_url when absent."""
    summary: dict[str, Any] = {
        "summary_250": "s",
        "summary_1000": "l",
        "tldr": "t",
        "metadata": {},
    }
    crawl_repo = SimpleNamespace(async_get_crawl_result_by_request=AsyncMock(return_value=None))
    request_repo = SimpleNamespace(
        async_get_request_by_id=AsyncMock(
            return_value={"normalized_url": "https://news.example.org/story"}
        ),
        async_update_request_status=AsyncMock(),
    )
    summary_repo = SimpleNamespace(
        async_persist_summary_with_llm_calls=AsyncMock(
            return_value=SummaryFinalizeResult(summary_id=99, version=1)
        ),
    )
    summary_index = SimpleNamespace(index_summary=AsyncMock())
    deps = _deps(
        crawl_repo=crawl_repo,
        requests=request_repo,
        summaries=summary_repo,
        summary_index=summary_index,
        llm_repo=None,
    )
    state = _state(summary=summary, request_id=42, lang="en")
    await persist(state, deps=deps)
    call_kwargs = summary_repo.async_persist_summary_with_llm_calls.call_args.kwargs
    saved = call_kwargs["json_payload"]
    assert "example.org" in saved.get("metadata", {}).get("domain", "")


async def test_gap4_persist_backfills_title_from_heading() -> None:
    """Title is extracted from the first markdown heading in source_text."""
    summary: dict[str, Any] = {
        "summary_250": "s",
        "summary_1000": "l",
        "tldr": "t",
        "metadata": {},
    }
    crawl_repo = SimpleNamespace(async_get_crawl_result_by_request=AsyncMock(return_value=None))
    request_repo = SimpleNamespace(
        async_get_request_by_id=AsyncMock(return_value=None),
        async_update_request_status=AsyncMock(),
    )
    summary_repo = SimpleNamespace(
        async_persist_summary_with_llm_calls=AsyncMock(
            return_value=SummaryFinalizeResult(summary_id=99, version=1)
        ),
    )
    summary_index = SimpleNamespace(index_summary=AsyncMock())
    deps = _deps(
        crawl_repo=crawl_repo,
        requests=request_repo,
        summaries=summary_repo,
        summary_index=summary_index,
        llm_repo=None,
    )
    state = _state(
        summary=summary,
        request_id=42,
        lang="en",
        source_text="# My Article Title\n\nBody text here.",
    )
    await persist(state, deps=deps)
    call_kwargs = summary_repo.async_persist_summary_with_llm_calls.call_args.kwargs
    saved = call_kwargs["json_payload"]
    assert saved.get("metadata", {}).get("title") == "My Article Title"


async def test_gap4_persist_skipped_when_crawl_repo_none() -> None:
    """No backfill when deps.crawl_repo is None; persist still completes."""
    summary: dict[str, Any] = {
        "summary_250": "s",
        "summary_1000": "l",
        "tldr": "t",
        "metadata": {},
    }
    summary_repo = SimpleNamespace(
        async_persist_summary_with_llm_calls=AsyncMock(
            return_value=SummaryFinalizeResult(summary_id=99, version=1)
        ),
    )
    summary_index = SimpleNamespace(index_summary=AsyncMock())
    deps = _deps(
        crawl_repo=None,
        requests=SimpleNamespace(async_update_request_status=AsyncMock()),
        summaries=summary_repo,
        summary_index=summary_index,
        llm_repo=None,
    )
    state = _state(summary=summary, request_id=42, lang="en")
    out = await persist(state, deps=deps)
    summary_repo.async_persist_summary_with_llm_calls.assert_awaited_once()


async def test_gap4_backfill_crawl_metadata_applied() -> None:
    """Firecrawl metadata_json title fills title field."""
    summary: dict[str, Any] = {"metadata": {}}
    crawl_repo = SimpleNamespace(
        async_get_crawl_result_by_request=AsyncMock(
            return_value={"metadata_json": {"title": "Scraped Title", "og:url": "https://x.com/a"}}
        )
    )
    request_repo = SimpleNamespace(async_get_request_by_id=AsyncMock(return_value=None))
    result = await backfill_summary_metadata(
        summary,
        request_id=1,
        content_text="",
        correlation_id="c",
        request_repo=request_repo,
        crawl_repo=crawl_repo,
    )
    assert result["metadata"]["title"] == "Scraped Title"
    assert result["metadata"]["canonical_url"] == "https://x.com/a"


# ---------------------------------------------------------------------------
# Unit helpers from metadata_backfill
# ---------------------------------------------------------------------------


def test_gap4_extract_heading_title_markdown() -> None:
    assert _extract_heading_title("# Hello World\n\nBody.") == "Hello World"


def test_gap4_extract_heading_title_fallback_first_line() -> None:
    assert _extract_heading_title("Short line\n\nMore text.") == "Short line"


def test_gap4_flatten_crawl_metadata_dict() -> None:
    row = {"metadata_json": {"title": "T", "author": "A"}}
    flat = _flatten_crawl_metadata(row)
    assert flat["title"] == "T"
    assert flat["author"] == "A"


# ===========================================================================
# GAP 5 — notify node docstring / DONE stage
# ===========================================================================


async def test_gap5_notify_returns_empty_dict() -> None:
    """notify is a clean no-op: returns {} always."""
    out = await notify(MagicMock(), deps=MagicMock())
    assert out == {}


async def test_gap5_notify_output_is_json_serializable() -> None:
    """notify output must be JSON-serializable (no leaked objects)."""
    out = await notify(MagicMock(), deps=MagicMock())
    assert json.loads(json.dumps(out)) == {}


def test_gap5_notify_in_bridge_stage_map() -> None:
    """GraphEventBridge maps 'notify' -> DONE (load-bearing topology check)."""
    from app.adapters.content.streaming.graph_event_bridge import _NODE_STAGE
    from app.application.dto.stream_enums import ProcessingStage

    assert _NODE_STAGE.get("notify") == ProcessingStage.DONE
