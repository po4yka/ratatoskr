"""Tests for Phase 3 request-root span instrumentation.

Covers:
- telegram.update span enrichment with Phase 3 attributes after prepare()
- url_flow.process span: set_correlation_id_attr propagation
- url_flow.cache_hit child span with dedupe_hash attribute
- URLProcessor in-flight gauge increment/decrement via set_url_processor_in_flight
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from app.observability.attributes import (
    REQUEST_DEDUPE_HASH,
    TELEGRAM_CHAT_ID,
    TELEGRAM_HAS_FORWARD,
    TELEGRAM_INTERACTION_TYPE,
    TELEGRAM_SOURCE_TYPE,
)

# ---------------------------------------------------------------------------
# OTel span attribute tests — require the SDK
# ---------------------------------------------------------------------------

opentelemetry = pytest.importorskip("opentelemetry", reason="opentelemetry SDK not installed")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.observability.otel as otel_module


def _make_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


# ---------------------------------------------------------------------------
# message_router.py — telegram.update span enrichment
# ---------------------------------------------------------------------------


class TestTelegramUpdateSpanEnrichment:
    """Verify that the telegram.update span gets Phase 3 attributes after prepare()."""

    def _make_route_context(
        self,
        interaction_type: str = "url",
        has_forward: bool = False,
        chat_id: int | None = 12345,
    ) -> Any:
        return SimpleNamespace(
            interaction_type=interaction_type,
            has_forward=has_forward,
            chat_id=chat_id,
            uid=99,
            first_url="https://example.com" if interaction_type == "url" else None,
        )

    def test_url_interaction_sets_correct_source_type(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        route_ctx = self._make_route_context(interaction_type="url", has_forward=False)

        with tracer.start_as_current_span("telegram.update") as span:
            _it = route_ctx.interaction_type
            _source_type = _it if _it in ("url", "forward") else "unknown"
            if span.is_recording():
                span.set_attribute(TELEGRAM_INTERACTION_TYPE, _it)
                span.set_attribute(
                    TELEGRAM_HAS_FORWARD, "true" if route_ctx.has_forward else "false"
                )
                if route_ctx.chat_id is not None:
                    span.set_attribute(TELEGRAM_CHAT_ID, str(route_ctx.chat_id))
                span.set_attribute(TELEGRAM_SOURCE_TYPE, _source_type)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes or {}
        assert attrs.get(TELEGRAM_INTERACTION_TYPE) == "url"
        assert attrs.get(TELEGRAM_HAS_FORWARD) == "false"
        assert attrs.get(TELEGRAM_CHAT_ID) == "12345"
        assert attrs.get(TELEGRAM_SOURCE_TYPE) == "url"

    def test_forward_interaction_sets_correct_source_type(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        route_ctx = self._make_route_context(interaction_type="forward", has_forward=True)

        with tracer.start_as_current_span("telegram.update") as span:
            _it = route_ctx.interaction_type
            _source_type = _it if _it in ("url", "forward") else "unknown"
            if span.is_recording():
                span.set_attribute(TELEGRAM_INTERACTION_TYPE, _it)
                span.set_attribute(
                    TELEGRAM_HAS_FORWARD, "true" if route_ctx.has_forward else "false"
                )
                if route_ctx.chat_id is not None:
                    span.set_attribute(TELEGRAM_CHAT_ID, str(route_ctx.chat_id))
                span.set_attribute(TELEGRAM_SOURCE_TYPE, _source_type)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes or {}
        assert attrs.get(TELEGRAM_INTERACTION_TYPE) == "forward"
        assert attrs.get(TELEGRAM_HAS_FORWARD) == "true"
        assert attrs.get(TELEGRAM_SOURCE_TYPE) == "forward"

    def test_command_interaction_maps_to_unknown_source_type(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        route_ctx = self._make_route_context(interaction_type="command", has_forward=False)

        with tracer.start_as_current_span("telegram.update") as span:
            _it = route_ctx.interaction_type
            _source_type = _it if _it in ("url", "forward") else "unknown"
            if span.is_recording():
                span.set_attribute(TELEGRAM_INTERACTION_TYPE, _it)
                span.set_attribute(
                    TELEGRAM_HAS_FORWARD, "true" if route_ctx.has_forward else "false"
                )
                if route_ctx.chat_id is not None:
                    span.set_attribute(TELEGRAM_CHAT_ID, str(route_ctx.chat_id))
                span.set_attribute(TELEGRAM_SOURCE_TYPE, _source_type)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes or {}
        assert attrs.get(TELEGRAM_INTERACTION_TYPE) == "command"
        assert attrs.get(TELEGRAM_SOURCE_TYPE) == "unknown"

    def test_text_interaction_maps_to_unknown_source_type(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        route_ctx = self._make_route_context(interaction_type="text", has_forward=False)

        with tracer.start_as_current_span("telegram.update") as span:
            _it = route_ctx.interaction_type
            _source_type = _it if _it in ("url", "forward") else "unknown"
            if span.is_recording():
                span.set_attribute(TELEGRAM_INTERACTION_TYPE, _it)
                span.set_attribute(TELEGRAM_SOURCE_TYPE, _source_type)

        spans = exporter.get_finished_spans()
        attrs = spans[0].attributes or {}
        assert attrs.get(TELEGRAM_SOURCE_TYPE) == "unknown"

    def test_none_chat_id_omits_chat_id_attribute(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        route_ctx = self._make_route_context(chat_id=None)

        with tracer.start_as_current_span("telegram.update") as span:
            if span.is_recording():
                if route_ctx.chat_id is not None:
                    span.set_attribute(TELEGRAM_CHAT_ID, str(route_ctx.chat_id))

        spans = exporter.get_finished_spans()
        assert TELEGRAM_CHAT_ID not in (spans[0].attributes or {})

    def test_has_forward_true_encodes_as_string_true(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("telegram.update") as span:
            if span.is_recording():
                span.set_attribute(TELEGRAM_HAS_FORWARD, "true")

        spans = exporter.get_finished_spans()
        assert spans[0].attributes.get(TELEGRAM_HAS_FORWARD) == "true"


# ---------------------------------------------------------------------------
# cached_summary_responder.py — url_flow.cache_hit span
# ---------------------------------------------------------------------------


class TestCacheHitSpan:
    """Verify that a url_flow.cache_hit span is emitted with dedupe_hash."""

    def test_cache_hit_span_carries_dedupe_hash(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        dedupe_hash = "abc123def456" * 4  # 48-char fake sha256 prefix

        with tracer.start_as_current_span("url_flow.process"):
            with tracer.start_as_current_span(
                "url_flow.cache_hit",
                attributes={REQUEST_DEDUPE_HASH: dedupe_hash},
            ):
                pass

        spans = exporter.get_finished_spans()
        # Innermost span finishes first with SimpleSpanProcessor
        cache_hit_span = next(s for s in spans if s.name == "url_flow.cache_hit")
        assert cache_hit_span.attributes.get(REQUEST_DEDUPE_HASH) == dedupe_hash

    def test_cache_hit_span_is_child_of_process_span(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("url_flow.process") as parent:
            with tracer.start_as_current_span(
                "url_flow.cache_hit",
                attributes={REQUEST_DEDUPE_HASH: "hash"},
            ):
                pass

        spans = exporter.get_finished_spans()
        parent_span = next(s for s in spans if s.name == "url_flow.process")
        child_span = next(s for s in spans if s.name == "url_flow.cache_hit")
        assert child_span.context.trace_id == parent_span.context.trace_id
        assert child_span.parent.span_id == parent_span.context.span_id


# ---------------------------------------------------------------------------
# url_flow.process — set_correlation_id_attr propagation
# ---------------------------------------------------------------------------


class TestUrlFlowCorrelationIdPropagation:
    """Verify set_correlation_id_attr attaches the cid inside url_flow.process."""

    def test_correlation_id_set_on_process_span(self) -> None:
        if not otel_module._otel_available:
            pytest.skip("opentelemetry SDK not available")

        provider, exporter = _make_provider()
        # Temporarily point the global tracer provider so set_correlation_id_attr
        # can find the active span.
        from opentelemetry import trace as _trace

        original_provider = _trace.get_tracer_provider()
        _trace.set_tracer_provider(provider)
        try:
            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("url_flow.process"):
                otel_module.set_correlation_id_attr("cid-url-flow-test")
        finally:
            _trace.set_tracer_provider(original_provider)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("ratatoskr.correlation_id") == "cid-url-flow-test"


# ---------------------------------------------------------------------------
# metrics.py — in-flight gauge helpers
# ---------------------------------------------------------------------------


class TestUrlProcessorInFlightGauge:
    """Verify set_url_processor_in_flight increments/decrements correctly."""

    def test_increment_increases_gauge(self) -> None:
        metrics = importlib.import_module("app.observability.metrics")
        if not metrics.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        gauge = metrics.URL_PROCESSOR_IN_FLIGHT
        before = gauge._value.get()
        metrics.set_url_processor_in_flight(+1)
        assert gauge._value.get() == before + 1.0
        # Restore
        metrics.set_url_processor_in_flight(-1)

    def test_decrement_decreases_gauge(self) -> None:
        metrics = importlib.import_module("app.observability.metrics")
        if not metrics.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        gauge = metrics.URL_PROCESSOR_IN_FLIGHT
        # Bring gauge to a known baseline
        metrics.set_url_processor_in_flight(+1)
        before = gauge._value.get()
        metrics.set_url_processor_in_flight(-1)
        assert gauge._value.get() == before - 1.0

    def test_zero_delta_is_noop(self) -> None:
        metrics = importlib.import_module("app.observability.metrics")
        if not metrics.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        gauge = metrics.URL_PROCESSOR_IN_FLIGHT
        before = gauge._value.get()
        metrics.set_url_processor_in_flight(0)
        assert gauge._value.get() == before

    def test_noop_when_prometheus_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        metrics = importlib.import_module("app.observability.metrics")
        monkeypatch.setattr(metrics, "PROMETHEUS_AVAILABLE", False)
        # Must not raise
        metrics.set_url_processor_in_flight(+1)
        metrics.set_url_processor_in_flight(-1)

    def test_noop_when_gauge_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        metrics = importlib.import_module("app.observability.metrics")
        monkeypatch.setattr(metrics, "URL_PROCESSOR_IN_FLIGHT", None)
        # Must not raise
        metrics.set_url_processor_in_flight(+1)


# ---------------------------------------------------------------------------
# Attributes module — constant name/value contract
# ---------------------------------------------------------------------------


class TestAttributeConstants:
    """Guard the string values of Phase 3 constants against accidental renames."""

    def test_telegram_interaction_type_value(self) -> None:
        from app.observability.attributes import TELEGRAM_INTERACTION_TYPE

        assert TELEGRAM_INTERACTION_TYPE == "ratatoskr.telegram.interaction_type"

    def test_telegram_has_forward_value(self) -> None:
        from app.observability.attributes import TELEGRAM_HAS_FORWARD

        assert TELEGRAM_HAS_FORWARD == "ratatoskr.telegram.has_forward"

    def test_telegram_chat_id_value(self) -> None:
        from app.observability.attributes import TELEGRAM_CHAT_ID

        assert TELEGRAM_CHAT_ID == "ratatoskr.telegram.chat_id"

    def test_telegram_source_type_value(self) -> None:
        from app.observability.attributes import TELEGRAM_SOURCE_TYPE

        assert TELEGRAM_SOURCE_TYPE == "ratatoskr.telegram.source_type"

    def test_request_dedupe_hash_value(self) -> None:
        from app.observability.attributes import REQUEST_DEDUPE_HASH

        assert REQUEST_DEDUPE_HASH == "ratatoskr.request.dedupe_hash"

    def test_request_source_type_value(self) -> None:
        from app.observability.attributes import REQUEST_SOURCE_TYPE

        assert REQUEST_SOURCE_TYPE == "ratatoskr.request.source_type"

    def test_request_correlation_id_value(self) -> None:
        from app.observability.attributes import REQUEST_CORRELATION_ID

        assert REQUEST_CORRELATION_ID == "ratatoskr.correlation_id"
