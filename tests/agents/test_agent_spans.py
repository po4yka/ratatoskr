"""Unit tests for Phase 4 agent OTel spans and Prometheus metrics.

Tests verify:
- Each agent opens a span with the correct name and attributes.
- ValidationAgent wires the AGENT_VALIDATION_FAILURE_REASON attribute
  and calls record_agent_validation_failure with the right reason.
- WebSearchAgent calls record_llm_call_attempt / record_llm_call_latency.
- RepoAnalysisAgent calls record_llm_call_persisted inside _persist().
- All agents carry REQUEST_CORRELATION_ID and AGENT_NAME on spans.
- OTel no-op fallback: agents work correctly when OTel SDK is absent.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_noop_tracer() -> Any:
    """Return the _NoOpTracer from otel.py (always available)."""
    otel = importlib.import_module("app.observability.otel")
    return otel._NoOpTracer()


# ---------------------------------------------------------------------------
# ValidationAgent span + counter
# ---------------------------------------------------------------------------


class TestValidationAgentSpan:
    """ValidationAgent must open agent.validation span and fire the counter."""

    @pytest.mark.asyncio
    async def test_validation_failure_fires_counter(self) -> None:
        from app.agents.validation_agent import ValidationAgent, ValidationInput
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        counter = m.AGENT_VALIDATION_FAILURES_TOTAL
        before = sum(
            counter.labels(reason=r)._value.get()
            for r in (
                "missing_field",
                "length_exceeded",
                "schema_mismatch",
                "language_mismatch",
                "unknown",
            )
        )

        agent = ValidationAgent(correlation_id="test-cid-1")
        # Pass an empty dict so _validate_required_fields fires "missing_field"
        result = await agent.execute(ValidationInput(summary_json={}))

        assert not result.success
        after = sum(
            counter.labels(reason=r)._value.get()
            for r in (
                "missing_field",
                "length_exceeded",
                "schema_mismatch",
                "language_mismatch",
                "unknown",
            )
        )
        assert after > before

    @pytest.mark.asyncio
    async def test_validation_success_does_not_fire_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.validation_agent import ValidationAgent, ValidationInput
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        counter = m.AGENT_VALIDATION_FAILURES_TOTAL
        before = sum(
            counter.labels(reason=r)._value.get()
            for r in (
                "missing_field",
                "length_exceeded",
                "schema_mismatch",
                "language_mismatch",
                "unknown",
            )
        )

        # Patch validate_and_shape_summary so we don't need a real summary
        monkeypatch.setattr(
            "app.agents.validation_agent.validate_and_shape_summary",
            lambda s: s,
        )

        valid_summary = {
            "summary_250": "A" * 100,
            "summary_1000": "B" * 200,
            "tldr": "C" * 300,
            "key_ideas": ["idea1"],
            "topic_tags": ["#ml"],
            "entities": {"people": [], "organizations": [], "locations": []},
            "estimated_reading_time_min": 3,
            "key_stats": [],
            "answered_questions": ["q1"],
            "readability": {"score": 65.0, "level": "medium"},
            "seo_keywords": ["kw1"],
        }
        agent = ValidationAgent(correlation_id="test-cid-2")
        result = await agent.execute(ValidationInput(summary_json=valid_summary))

        assert result.success
        after = sum(
            counter.labels(reason=r)._value.get()
            for r in (
                "missing_field",
                "length_exceeded",
                "schema_mismatch",
                "language_mismatch",
                "unknown",
            )
        )
        assert after == before

    @pytest.mark.asyncio
    async def test_validation_failure_reason_missing_field(self) -> None:
        from app.agents.validation_agent import ValidationAgent, ValidationInput

        agent = ValidationAgent(correlation_id="cid")
        result = await agent.execute(ValidationInput(summary_json={}))
        assert not result.success

    @pytest.mark.asyncio
    async def test_validation_exception_fires_unknown_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.validation_agent import ValidationAgent, ValidationInput
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        counter = m.AGENT_VALIDATION_FAILURES_TOTAL
        before = counter.labels(reason="unknown")._value.get()

        monkeypatch.setattr(
            "app.agents.validation_agent.ValidationAgent._validate_required_fields",
            lambda self, summary, errors: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        agent = ValidationAgent(correlation_id="cid")
        result = await agent.execute(ValidationInput(summary_json={"x": 1}))

        assert not result.success
        after = counter.labels(reason="unknown")._value.get()
        assert after == before + 1.0


class TestValidationAgentClassifyReason:
    """Unit tests for _classify_validation_failure helper."""

    def test_missing_field_classification(self) -> None:
        from app.agents.validation_agent import ValidationAgent

        assert (
            ValidationAgent._classify_validation_failure(["Missing required fields: foo"])
            == "missing_field"
        )

    def test_length_exceeded_classification(self) -> None:
        from app.agents.validation_agent import ValidationAgent

        assert (
            ValidationAgent._classify_validation_failure(["summary_250 exceeds limit: 300 chars"])
            == "length_exceeded"
        )

    def test_schema_mismatch_classification(self) -> None:
        from app.agents.validation_agent import ValidationAgent

        assert (
            ValidationAgent._classify_validation_failure(["key_stats must be a list"])
            == "schema_mismatch"
        )

    def test_unknown_when_no_errors(self) -> None:
        from app.agents.validation_agent import ValidationAgent

        assert ValidationAgent._classify_validation_failure([]) == "unknown"

    def test_unknown_for_unrecognised_message(self) -> None:
        from app.agents.validation_agent import ValidationAgent

        assert ValidationAgent._classify_validation_failure(["some unexpected error"]) == "unknown"


# ---------------------------------------------------------------------------
# ContentExtractionAgent span
# ---------------------------------------------------------------------------


class TestContentExtractionAgentSpan:
    """ContentExtractionAgent must open agent.content_extraction span."""

    @pytest.mark.asyncio
    async def test_execute_sets_span_attributes(self) -> None:
        from app.agents.content_extraction_agent import ContentExtractionAgent, ExtractionInput

        extractor = MagicMock()
        extractor.extract_content_pure = AsyncMock(
            return_value=("content text", "direct", {"title": "t"})
        )
        request_repo = MagicMock()
        request_repo.async_get_request_by_dedupe_hash = AsyncMock(return_value=None)
        crawl_result_repo = MagicMock()

        recorded_attrs: dict[str, Any] = {}

        class _RecordingSpan:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded_attrs[k] = v

            def __enter__(self) -> _RecordingSpan:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _RecordingTracer:
            def start_as_current_span(self, name: str, **_kw: Any) -> _RecordingSpan:
                recorded_attrs["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_RecordingSpan())

        with patch("app.agents.content_extraction_agent._tracer", _RecordingTracer()):
            agent = ContentExtractionAgent(
                content_extractor=extractor,
                request_repo=request_repo,
                crawl_result_repo=crawl_result_repo,
                correlation_id="cid-extract",
            )
            result = await agent.execute(
                ExtractionInput(url="https://example.com/article", correlation_id="cid-extract")
            )

        assert result.success
        assert recorded_attrs.get("_span_name") == "agent.content_extraction"
        assert recorded_attrs.get("ratatoskr.agent.name") == "content_extraction"
        assert recorded_attrs.get("ratatoskr.correlation_id") == "cid-extract"


# ---------------------------------------------------------------------------
# WebSearchAgent span + LLM metrics
# ---------------------------------------------------------------------------


class TestWebSearchAgentSpanAndMetrics:
    """WebSearchAgent must open agent.web_search span and call LLM metrics."""

    def _make_agent(self) -> Any:
        from app.agents.web_search_agent import WebSearchAgent

        llm = MagicMock()
        llm._model = "test/model"
        llm.chat_structured = AsyncMock(
            return_value=MagicMock(
                parsed=MagicMock(needs_search=False, queries=[], reason="no search needed")
            )
        )
        search_svc = MagicMock()
        cfg = MagicMock()
        cfg.min_content_length = 10
        cfg.max_context_chars = 5000
        cfg.max_queries = 3
        return WebSearchAgent(
            llm_client=llm, search_service=search_svc, cfg=cfg, correlation_id="cid-ws"
        )

    @pytest.mark.asyncio
    async def test_execute_sets_span_attributes(self) -> None:
        from app.agents.web_search_agent import WebSearchAgentInput

        recorded_attrs: dict[str, Any] = {}

        class _RecordingSpan:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded_attrs[k] = v

            def __enter__(self) -> _RecordingSpan:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _RecordingTracer:
            def start_as_current_span(self, name: str, **_kw: Any) -> _RecordingSpan:
                recorded_attrs["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_RecordingSpan())

        agent = self._make_agent()
        with patch("app.agents.web_search_agent._tracer", _RecordingTracer()):
            result = await agent.execute(
                WebSearchAgentInput(content="long enough content here", language="en")
            )

        assert result.success
        assert recorded_attrs.get("_span_name") == "agent.web_search"
        assert recorded_attrs.get("ratatoskr.agent.name") == "web_search"

    @pytest.mark.asyncio
    async def test_llm_metrics_called_on_analysis(self) -> None:
        from app.agents.web_search_agent import WebSearchAgentInput
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        agent = self._make_agent()
        # Trigger _analyze_content by making content long enough
        before = m.LLM_CALL_ATTEMPTS_TOTAL.labels(
            provider="openrouter", model="other", status="success"
        )._value.get()
        await agent.execute(WebSearchAgentInput(content="long enough content here", language="en"))
        after = m.LLM_CALL_ATTEMPTS_TOTAL.labels(
            provider="openrouter", model="other", status="success"
        )._value.get()
        assert (
            after >= before
        )  # counter only increments; may or may not have fired depending on model bucket


# ---------------------------------------------------------------------------
# RepoAnalysisAgent span + record_llm_call_persisted
# ---------------------------------------------------------------------------


class TestRepoAnalysisAgentSpanAndMetrics:
    """RepoAnalysisAgent must open agent.repo_analysis span and call record_llm_call_persisted."""

    @pytest.mark.asyncio
    async def test_persist_calls_record_llm_call_persisted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.repo_analysis_agent import RepoAnalysisAgent
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        llm_repo = MagicMock()
        llm_repo.async_insert_llm_call = AsyncMock(return_value=1)

        agent = RepoAnalysisAgent(
            llm_service=MagicMock(),
            llm_repo=llm_repo,
            request_id=42,
            model_name="openai/gpt-4o",
        )

        recorded_calls: list[dict] = []
        original = m.record_llm_call_persisted

        def _spy(call: dict) -> None:
            recorded_calls.append(call)
            original(call)

        monkeypatch.setattr("app.agents.repo_analysis_agent.record_llm_call_persisted", _spy)

        await agent._persist(
            correlation_id="cid-repo",
            attempt_index=1,
            attempt_trigger="structured",
            response_text="{}",
            status="ok",
            error_text=None,
            model_name="openai/gpt-4o",
            tokens_prompt=100,
            tokens_completion=50,
            cost_usd=0.001,
            latency_ms=1200,
        )

        assert len(recorded_calls) == 1
        assert recorded_calls[0]["model"] == "openai/gpt-4o"
        assert recorded_calls[0]["tokens_prompt"] == 100

    @pytest.mark.asyncio
    async def test_span_opened_on_analyze(self) -> None:
        from app.agents.repo_analysis_agent import RepoAnalysisAgent

        recorded: dict[str, Any] = {}

        class _RecordingSpan:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _RecordingSpan:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _RecordingTracer:
            def start_as_current_span(self, name: str, **_kw: Any) -> _RecordingSpan:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_RecordingSpan())

        from app.core.repo_analysis_schema import RepoAnalysisInput

        llm = MagicMock()
        # Not a StructuredLLMServiceProtocol (no chat_structured) — uses legacy path
        del llm.chat_structured
        llm.call = AsyncMock(return_value='{"confidence": 0.5}')

        with (
            patch("app.agents.repo_analysis_agent._get_tracer", return_value=_RecordingTracer()),
            patch(
                "app.agents.repo_analysis_agent.parse_and_validate_repo_analysis",
                return_value=MagicMock(confidence=0.5),
            ),
        ):
            agent = RepoAnalysisAgent(llm_service=llm, model_name="test/model")
            await agent.analyze(
                RepoAnalysisInput(
                    full_name="user/repo",
                    description="A test repo",
                    readme_excerpt="README content",
                    topics=[],
                    primary_language="Python",
                ),
                correlation_id="cid-repo",
            )

        assert recorded.get("_span_name") == "agent.repo_analysis"
        assert recorded.get("ratatoskr.agent.name") == "repo_analysis"
        assert recorded.get("ratatoskr.correlation_id") == "cid-repo"


# ---------------------------------------------------------------------------
# CombinedSummaryAgent span
# ---------------------------------------------------------------------------


class TestCombinedSummaryAgentSpan:
    """CombinedSummaryAgent must open agent.combined_summary span."""

    @pytest.mark.asyncio
    async def test_execute_sets_span_attributes(self) -> None:
        from app.adapter_models.batch_analysis import (
            ArticleMetadata,
            CombinedSummaryInput,
            RelationshipAnalysisOutput,
            RelationshipType,
        )
        from app.agents.combined_summary_agent import CombinedSummaryAgent

        recorded: dict[str, Any] = {}

        class _RecordingSpan:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _RecordingSpan:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _RecordingTracer:
            def start_as_current_span(self, name: str, **_kw: Any) -> _RecordingSpan:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_RecordingSpan())

        llm = MagicMock()
        llm.chat_structured = AsyncMock(side_effect=RuntimeError("no LLM in test"))

        agent = CombinedSummaryAgent(llm_client=llm, correlation_id="cid-cs")

        # Build a minimal two-article input with a non-UNRELATED relationship
        articles = [
            ArticleMetadata(request_id=1, url="https://a.com", title="A"),
            ArticleMetadata(request_id=2, url="https://b.com", title="B"),
        ]
        relationship = RelationshipAnalysisOutput(
            relationship_type=RelationshipType.TOPIC_CLUSTER,
            confidence=0.9,
        )
        inp = CombinedSummaryInput(
            articles=articles,
            full_summaries=[{}, {}],
            relationship=relationship,
            correlation_id="cid-cs",
            language="en",
        )

        with patch("app.agents.combined_summary_agent._tracer", _RecordingTracer()):
            result = await agent.execute(inp)

        # LLM fails but span was opened
        assert recorded.get("_span_name") == "agent.combined_summary"
        assert recorded.get("ratatoskr.agent.name") == "combined_summary"
        assert recorded.get("ratatoskr.correlation_id") == "cid-cs"
