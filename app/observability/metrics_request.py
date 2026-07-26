"""Prometheus metrics for request throughput and URL worker queue.

Covers:
- Inbound request counts and latency (REQUESTS_TOTAL, REQUEST_LATENCY)
- URL enqueue outcomes (URL_ENQUEUE_TOTAL)
- URL processing queue depth and in-flight gauge
"""

from __future__ import annotations

from app.observability._metrics_base import (
    PROMETHEUS_AVAILABLE,
    REGISTRY,
    _metric_label,
)

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    REQUESTS_TOTAL = Counter(
        "ratatoskr_requests_total",
        "Total number of requests processed",
        ["type", "status", "source"],
        registry=REGISTRY,
    )

    REQUEST_LATENCY = Histogram(
        "ratatoskr_request_latency_seconds",
        "Request latency in seconds",
        ["type", "stage"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
        registry=REGISTRY,
    )

    # Incremented by the bot each time it enqueues (or falls back to inline).
    URL_ENQUEUE_TOTAL = Counter(
        "ratatoskr_url_enqueue_total",
        "Bot URL enqueue outcomes",
        ["status"],
        registry=REGISTRY,
    )

    # Current depth of the URL processing queue (pending + failed-retryable).
    URL_PROCESSING_QUEUE_DEPTH = Gauge(
        "ratatoskr_url_processing_queue_depth",
        "Number of URL processing jobs waiting in the queue",
        multiprocess_mode="mostrecent",
        registry=REGISTRY,
    )

    # Incremented when URLProcessor begins processing a request; decremented
    # in the finally-block. Multiprocess mode sums concurrent Taskiq workers.
    URL_PROCESSOR_IN_FLIGHT = Gauge(
        "ratatoskr_url_processor_in_flight",
        "Number of URL processing requests currently active",
        multiprocess_mode="livesum",
        registry=REGISTRY,
    )

else:
    REQUESTS_TOTAL = None
    REQUEST_LATENCY = None
    URL_ENQUEUE_TOTAL = None
    URL_PROCESSING_QUEUE_DEPTH = None
    URL_PROCESSOR_IN_FLIGHT = None


def record_request(
    request_type: str,
    status: str,
    source: str,
    latency_seconds: float | None = None,
    stage: str = "total",
) -> None:
    """Record a request metric.

    Args:
        request_type: Type of request (url, forward, command)
        status: Request status (success, error, timeout)
        source: Request source (telegram, api, cli)
        latency_seconds: Optional latency in seconds
        stage: Processing stage (extraction, summarization, total)
    """
    if not PROMETHEUS_AVAILABLE:
        return

    REQUESTS_TOTAL.labels(type=request_type, status=status, source=source).inc()

    if latency_seconds is not None:
        REQUEST_LATENCY.labels(type=request_type, stage=stage).observe(latency_seconds)


def record_url_enqueue(*, status: str) -> None:
    """Record a bot URL enqueue outcome.

    Args:
        status: ``success`` | ``skipped_inline`` | ``failed``
    """
    if not PROMETHEUS_AVAILABLE or URL_ENQUEUE_TOTAL is None:
        return
    URL_ENQUEUE_TOTAL.labels(status=_metric_label(status)).inc()


def set_url_processing_queue_depth(depth: int) -> None:
    """Update the URL processing queue depth gauge."""
    if not PROMETHEUS_AVAILABLE or URL_PROCESSING_QUEUE_DEPTH is None:
        return
    if depth < 0:
        return
    URL_PROCESSING_QUEUE_DEPTH.set(depth)


def set_url_processor_in_flight(delta: int) -> None:
    """Increment or decrement the URL processor in-flight gauge.

    Call with delta=+1 when URLProcessor begins a request and delta=-1 in
    the finally-block when it completes.  Uses inc()/dec() to be safe under
    concurrent calls from the same process.

    Args:
        delta: +1 to increment (request started), -1 to decrement (request done).
    """
    if not PROMETHEUS_AVAILABLE or URL_PROCESSOR_IN_FLIGHT is None:
        return
    if delta > 0:
        URL_PROCESSOR_IN_FLIGHT.inc(delta)
    elif delta < 0:
        URL_PROCESSOR_IN_FLIGHT.dec(-delta)
