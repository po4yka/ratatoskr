"""Tests for init_tracing() idempotency and no-op behavior."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

import app.observability.otel as otel_module


def test_init_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_module, "_initialized", False)
    monkeypatch.setenv("OTEL_ENABLED", "false")
    otel_module.init_tracing()
    assert not otel_module._initialized


def test_http_header_sanitizers_include_token_like_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS",
        "custom-header,authorization",
    )

    otel_module._ensure_http_header_sanitizers()

    configured = os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"].split(",")
    assert configured.count("authorization") == 1
    assert "custom-header" in configured
    assert "x-github-token" in configured
    assert ".*token.*" in configured
    assert ".*secret.*" in configured


def test_init_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call short-circuits before _is_enabled is ever checked."""
    monkeypatch.setattr(otel_module, "_initialized", True)
    reached: list[bool] = []
    original = otel_module._is_enabled

    def _spy(cfg: object) -> bool:
        reached.append(True)
        return original(cfg)  # type: ignore[arg-type]

    monkeypatch.setattr(otel_module, "_is_enabled", _spy)
    otel_module.init_tracing()
    assert reached == []


def test_get_tracer_returns_noop_when_sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_module, "_otel_available", False)
    tracer = otel_module.get_tracer("test.module")
    assert isinstance(tracer, otel_module._NoOpTracer)


def test_noop_tracer_start_as_current_span_is_context_manager() -> None:
    tracer = otel_module._NoOpTracer()
    with tracer.start_as_current_span("test.span"):
        pass  # must not raise


def test_noop_span_methods_are_safe() -> None:
    span = otel_module._NoOpSpan()
    span.set_attribute("key", "value")
    span.record_exception(ValueError("boom"))
    span.set_status(None)
    assert not span.is_recording()
    with span:
        pass  # context manager must work
