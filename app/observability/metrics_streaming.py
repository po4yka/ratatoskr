"""Prometheus metrics for draft streaming and stream latency.

Covers:
- Draft/stream lifecycle event counter (DRAFT_STREAM_EVENTS)
- Streaming timing metrics in milliseconds (STREAM_LATENCY_MS)
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Histogram

    DRAFT_STREAM_EVENTS = Counter(
        "ratatoskr_draft_stream_events_total",
        "Draft/stream lifecycle events",
        ["event"],
        registry=REGISTRY,
    )

    STREAM_LATENCY_MS = Histogram(
        "ratatoskr_stream_latency_ms",
        "Streaming timing metrics in milliseconds",
        ["metric"],
        buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000],
        registry=REGISTRY,
    )

else:
    DRAFT_STREAM_EVENTS = None
    STREAM_LATENCY_MS = None


def record_draft_stream_event(event: str, *, amount: int = 1) -> None:
    """Record a draft/stream event counter."""
    if not PROMETHEUS_AVAILABLE:
        return
    if amount <= 0:
        return
    DRAFT_STREAM_EVENTS.labels(event=event).inc(amount)


def record_stream_latency_ms(metric: str, value_ms: float) -> None:
    """Record stream latency-like metric in milliseconds."""
    if not PROMETHEUS_AVAILABLE:
        return
    if value_ms < 0:
        return
    STREAM_LATENCY_MS.labels(metric=metric).observe(value_ms)
