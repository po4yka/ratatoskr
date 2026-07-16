"""Bounded RED metrics for FastAPI HTTP requests."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    HTTP_REQUESTS_TOTAL = Counter(
        "ratatoskr_http_requests_total",
        "FastAPI HTTP requests by route template, method, and status class.",
        ["route", "method", "status_class"],
        registry=REGISTRY,
    )
    HTTP_REQUEST_DURATION_SECONDS = Histogram(
        "ratatoskr_http_request_duration_seconds",
        "FastAPI HTTP request duration by route template and method.",
        ["route", "method"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
        registry=REGISTRY,
    )
    HTTP_REQUESTS_IN_FLIGHT = Gauge(
        "ratatoskr_http_requests_in_flight",
        "FastAPI HTTP requests currently in flight by method.",
        ["method"],
        multiprocess_mode="livesum",
        registry=REGISTRY,
    )
else:
    HTTP_REQUESTS_TOTAL = None
    HTTP_REQUEST_DURATION_SECONDS = None
    HTTP_REQUESTS_IN_FLIGHT = None


def bucket_http_method(method: str) -> str:
    normalized = method.upper()
    return normalized if normalized in _METHODS else "OTHER"


def bucket_status_class(status_code: int) -> str:
    label = f"{status_code // 100}xx"
    return label if label in _STATUS_CLASSES else "unknown"


def change_http_in_flight(method: str, amount: int) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    HTTP_REQUESTS_IN_FLIGHT.labels(method=bucket_http_method(method)).inc(amount)


def record_http_request(
    *, route: str, method: str, status_code: int, duration_seconds: float
) -> None:
    """Record a completed request using server-derived bounded labels."""
    if not PROMETHEUS_AVAILABLE:
        return
    method_label = bucket_http_method(method)
    status_class = bucket_status_class(status_code)
    HTTP_REQUESTS_TOTAL.labels(route=route, method=method_label, status_class=status_class).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(route=route, method=method_label).observe(
        max(0.0, duration_seconds)
    )
