"""Small tracing helpers for application use cases."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from app.observability.attributes import REQUEST_CORRELATION_ID, REQUEST_USER_ID, USE_CASE_NAME


def _value_from_source(source: Any, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _first_value(key: str, sources: tuple[Any, ...], explicit: Any = None) -> Any:
    if explicit is not None:
        return explicit
    for source in sources:
        value = _value_from_source(source, key)
        if value is not None:
            return value
    return None


@contextmanager
def use_case_span(
    name: str,
    *sources: Any,
    user_id: int | str | None = None,
    correlation_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Any:
    """Open a top-level application use-case span with standard identity attrs."""
    from app.observability.otel import get_tracer

    resolved_user_id = _first_value("user_id", sources, user_id)
    resolved_correlation_id = _first_value("correlation_id", sources, correlation_id)
    span_attributes: dict[str, Any] = {USE_CASE_NAME: name}
    if resolved_user_id is not None:
        span_attributes[REQUEST_USER_ID] = resolved_user_id
    if resolved_correlation_id:
        span_attributes[REQUEST_CORRELATION_ID] = resolved_correlation_id
    if attributes:
        span_attributes.update(attributes)

    with get_tracer(__name__).start_as_current_span(
        f"use_case.{name}",
        attributes=span_attributes,
    ) as span:
        yield span
