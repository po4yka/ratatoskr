from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from app.application.use_cases._tracing import use_case_span
from app.observability.attributes import REQUEST_CORRELATION_ID, REQUEST_USER_ID, USE_CASE_NAME

pytestmark = pytest.mark.no_network


class _RecordingSpan:
    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.name = name
        self.attrs = dict(attributes or {})

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def is_recording(self) -> bool:
        return True

    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _RecordingTracer:
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_as_current_span(
        self, name: str, attributes: dict[str, Any] | None = None, **_kwargs: Any
    ) -> _RecordingSpan:
        span = _RecordingSpan(name, attributes)
        self.spans.append(span)
        return span


@dataclass(frozen=True)
class _Command:
    user_id: int
    correlation_id: str


def test_use_case_span_extracts_standard_attrs_from_command() -> None:
    tracer = _RecordingTracer()

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with use_case_span("example.execute", _Command(user_id=42, correlation_id="cid-1")):
            pass

    span = tracer.spans[0]
    assert span.name == "use_case.example.execute"
    assert span.attrs[USE_CASE_NAME] == "example.execute"
    assert span.attrs[REQUEST_USER_ID] == 42
    assert span.attrs[REQUEST_CORRELATION_ID] == "cid-1"


def test_use_case_span_explicit_attrs_override_source_attrs() -> None:
    tracer = _RecordingTracer()

    with patch("app.observability.otel.get_tracer", return_value=tracer):
        with use_case_span(
            "example.override",
            {"user_id": 1, "correlation_id": "from-source"},
            user_id=2,
            correlation_id="explicit",
            attributes={"ratatoskr.example.attr": "value"},
        ):
            pass

    span = tracer.spans[0]
    assert span.attrs[REQUEST_USER_ID] == 2
    assert span.attrs[REQUEST_CORRELATION_ID] == "explicit"
    assert span.attrs["ratatoskr.example.attr"] == "value"
