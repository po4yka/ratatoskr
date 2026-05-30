"""Tests for OTelPropagationMiddleware — W3C traceparent round-trip across broker hop."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("opentelemetry", reason="opentelemetry SDK not installed")
pytest.importorskip("taskiq", reason="taskiq not installed")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.tasks.middleware import OTelPropagationMiddleware


class _FakeMessage:
    """Minimal stand-in for taskiq.message.TaskiqMessage."""

    def __init__(
        self, *, task_name: str = "my_task", task_id: str = "tid-123", kwargs: dict | None = None
    ) -> None:
        self.labels: dict[str, Any] = {}
        self.task_name = task_name
        self.task_id = task_id
        self.kwargs = kwargs or {}


class _FakeResult:
    def __init__(self, is_err: bool = False) -> None:
        self.is_err = is_err


def _make_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Set as global provider so middleware's trace.get_tracer() uses it
    trace.set_tracer_provider(provider)
    return provider, exporter


@pytest.mark.asyncio
async def test_pre_send_injects_traceparent() -> None:
    """pre_send must inject W3C traceparent into message.labels when there is an active span."""
    provider, _ = _make_provider()
    tracer = provider.get_tracer("test")
    middleware = OTelPropagationMiddleware()
    message = _FakeMessage()

    with tracer.start_as_current_span("producer.root"):
        result = await middleware.pre_send(message)  # type: ignore[arg-type]

    assert result is message
    assert "traceparent" in message.labels


@pytest.mark.asyncio
async def test_pre_execute_creates_child_span() -> None:
    """pre_execute must start a child span and store it on the message."""
    from unittest.mock import patch

    provider, exporter = _make_provider()
    tracer = provider.get_tracer("test")
    middleware = OTelPropagationMiddleware()

    # Producer side: inject into labels while inside a parent span
    producer_message = _FakeMessage()
    with tracer.start_as_current_span("producer.root") as parent:
        parent_ctx = trace.get_current_span().get_span_context()
        await middleware.pre_send(producer_message)  # type: ignore[arg-type]

    # Worker side: extract context and start child span.
    # Patch trace.get_tracer in the middleware module so it uses this test's
    # isolated provider instead of the global one (avoids parallel-test bleed).
    worker_message = _FakeMessage(task_name="my_task")
    worker_message.labels = dict(producer_message.labels)
    with patch("opentelemetry.trace") as mock_trace:
        mock_trace.get_tracer.return_value = tracer
        mock_trace.get_current_span = trace.get_current_span
        await middleware.pre_execute(worker_message)  # type: ignore[arg-type]

    span = getattr(worker_message, "_otel_span", None)
    assert span is not None
    assert span.is_recording()

    # Finish — post_execute ends the span
    await middleware.post_execute(worker_message, _FakeResult(is_err=False))  # type: ignore[arg-type]

    finished = exporter.get_finished_spans()
    child_spans = [s for s in finished if s.name == "taskiq.my_task"]
    assert len(child_spans) == 1
    assert child_spans[0].parent is not None
    assert child_spans[0].parent.trace_id == parent_ctx.trace_id


@pytest.mark.asyncio
async def test_post_execute_sets_is_err_attribute() -> None:
    """post_execute must record ratatoskr.task.is_err on the span."""
    from unittest.mock import patch

    provider, exporter = _make_provider()
    tracer = provider.get_tracer("test")
    middleware = OTelPropagationMiddleware()

    message = _FakeMessage()
    with patch("opentelemetry.trace") as mock_trace:
        mock_trace.get_tracer.return_value = tracer
        mock_trace.get_current_span = trace.get_current_span
        await middleware.pre_execute(message)  # type: ignore[arg-type]  # creates _otel_span from empty labels

    result = _FakeResult(is_err=True)
    await middleware.post_execute(message, result)  # type: ignore[arg-type]

    finished = exporter.get_finished_spans()
    assert any(s.attributes.get("ratatoskr.task.is_err") is True for s in finished)


@pytest.mark.asyncio
async def test_middleware_is_noop_without_active_span() -> None:
    """pre_send when no span is active must not raise and must return the message."""
    middleware = OTelPropagationMiddleware()
    message = _FakeMessage()
    result = await middleware.pre_send(message)  # type: ignore[arg-type]
    assert result is message
