"""Prometheus metrics for vector store operations.

Covers:
- Vector indexing reconciliation lag and drift counts (VECTOR_INDEXING_LAG)
- Vector store write outcomes (VECTOR_WRITES_TOTAL)
"""

from __future__ import annotations

from typing import Any

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge

    VECTOR_INDEXING_LAG = Gauge(
        "ratatoskr_vector_indexing_lag",
        "Vector indexing reconciliation lag and drift counts",
        ["metric"],
        registry=REGISTRY,
    )

    VECTOR_WRITES_TOTAL = Counter(
        "ratatoskr_vector_writes_total",
        "Vector store write attempts by operation and status",
        ["operation", "status"],
        registry=REGISTRY,
    )

else:
    VECTOR_INDEXING_LAG = None
    VECTOR_WRITES_TOTAL = None


def record_vector_index_lag(report: dict[str, Any]) -> None:
    """Record vector reconciliation gauges from a diagnostics report."""
    if not PROMETHEUS_AVAILABLE or VECTOR_INDEXING_LAG is None:
        return
    for metric in (
        "lag_seconds",
        "missing_summary_vectors",
        "missing_repository_vectors",
        "stale_embedding_model_count",
        "missing_embeddings",
        "stale_embeddings",
    ):
        value = report.get(metric)
        if value is None:
            continue
        VECTOR_INDEXING_LAG.labels(metric=metric).set(float(value))


def record_vector_write(*, operation: str, status: str) -> None:
    """Record a vector-store write outcome."""
    if not PROMETHEUS_AVAILABLE or VECTOR_WRITES_TOTAL is None:
        return
    VECTOR_WRITES_TOTAL.labels(operation=operation, status=status).inc()
