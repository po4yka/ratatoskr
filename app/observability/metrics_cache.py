"""Prometheus metrics for the shared Redis JSON cache."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

_KNOWN_CACHE_OPERATIONS = frozenset({"get", "set", "clear"})
_KNOWN_CACHE_OUTCOMES = frozenset({"hit", "miss", "success", "error"})
_KNOWN_CACHE_NAMESPACE_FAMILIES = frozenset(
    {"all", "auth", "batch", "embed", "fc", "llm", "trending", "url"}
)

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Histogram

    REDIS_CACHE_OPERATIONS_TOTAL = Counter(
        "ratatoskr_redis_cache_operations_total",
        "Redis cache operations by bounded namespace family and outcome",
        ["operation", "outcome", "namespace"],
        registry=REGISTRY,
    )
    REDIS_CACHE_OPERATION_LATENCY_SECONDS = Histogram(
        "ratatoskr_redis_cache_operation_latency_seconds",
        "Redis cache operation latency in seconds",
        ["operation", "namespace"],
        buckets=[0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
        registry=REGISTRY,
    )
else:
    REDIS_CACHE_OPERATIONS_TOTAL = None
    REDIS_CACHE_OPERATION_LATENCY_SECONDS = None


def cache_namespace_family(value: str | None) -> str:
    """Collapse a cache key's first component into a bounded label value."""
    normalized = str(value or "").strip().lower()
    if normalized in _KNOWN_CACHE_NAMESPACE_FAMILIES:
        return normalized
    return "other"


def record_redis_cache_operation(
    *,
    operation: str,
    outcome: str,
    namespace: str,
    latency_seconds: float,
) -> None:
    """Record one cache operation without exposing the Redis key in labels."""
    if not PROMETHEUS_AVAILABLE:
        return

    operation_label = operation if operation in _KNOWN_CACHE_OPERATIONS else "other"
    outcome_label = outcome if outcome in _KNOWN_CACHE_OUTCOMES else "error"
    namespace_label = cache_namespace_family(namespace)
    REDIS_CACHE_OPERATIONS_TOTAL.labels(
        operation=operation_label,
        outcome=outcome_label,
        namespace=namespace_label,
    ).inc()
    REDIS_CACHE_OPERATION_LATENCY_SECONDS.labels(
        operation=operation_label,
        namespace=namespace_label,
    ).observe(max(0.0, latency_seconds))
