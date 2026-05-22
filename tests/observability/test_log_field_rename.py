"""Tests for _json_sink OTel field rename: otelTraceID → trace_id."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


def _make_loguru_message(extra: dict[str, Any], msg: str = "hello") -> Any:
    """Build a minimal fake loguru message record understood by _json_sink."""
    now = datetime.now(tz=timezone.utc)
    record: dict[str, Any] = {
        "time": now,
        "level": SimpleNamespace(name="INFO"),
        "name": "test.logger",
        "message": msg,
        "module": "test_module",
        "function": "test_func",
        "line": 1,
        "process": SimpleNamespace(id=99),
        "thread": SimpleNamespace(id=1),
        "extra": extra,
        "exception": None,
    }
    return SimpleNamespace(record=record)


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def _emit(extra: dict[str, Any], monkeypatch: Any) -> dict[str, Any]:
    from app.core.logging_utils import _json_sink

    fake = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake)
    _json_sink(_make_loguru_message(extra))
    fake.buffer.seek(0)
    return json.loads(fake.buffer.read().strip())


def test_otel_trace_id_renamed_to_snake_case(monkeypatch: Any) -> None:
    data = _emit(
        {
            "otelTraceID": "abc123abc123abc123abc123abc123ab",
            "otelSpanID": "deadbeef12345678",
            "otelTraceSampled": True,
            "otelServiceName": "ratatoskr",
        },
        monkeypatch,
    )
    assert data["trace_id"] == "abc123abc123abc123abc123abc123ab"
    assert data["span_id"] == "deadbeef12345678"
    assert "otelTraceID" not in data
    assert "otelTraceSampled" not in data
    assert "otelServiceName" not in data


def test_zero_trace_id_not_renamed(monkeypatch: Any) -> None:
    """otelTraceID='0' means 'no active span' — must not create a trace_id field."""
    data = _emit({"otelTraceID": "0", "otelSpanID": "0"}, monkeypatch)
    assert "trace_id" not in data


def test_record_without_otel_fields_unchanged(monkeypatch: Any) -> None:
    data = _emit({"correlation_id": "my-cid-789"}, monkeypatch)
    assert data["correlation_id"] == "my-cid-789"
    assert "trace_id" not in data
    assert "span_id" not in data


def test_json_sink_redacts_sensitive_extra_fields(monkeypatch: Any) -> None:
    data = _emit(
        {
            "Authorization": "Bearer sk-or-secretsecretsecret",
            "source_url": "https://example.test/private/path?token=secret",
            "text_preview": "private source body",
            "provider": "openrouter",
            "latency_ms": 42,
        },
        monkeypatch,
    )

    rendered = json.dumps(data)
    assert "sk-or-secretsecretsecret" not in rendered
    assert "/private/path" not in rendered
    assert "private source body" not in rendered
    assert data["Authorization"] == "[REDACTED]"
    assert data["text_preview"] == "[REDACTED_CONTENT]"
    assert data["provider"] == "openrouter"
    assert data["latency_ms"] == 42
