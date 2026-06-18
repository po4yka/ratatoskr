"""Prometheus metrics for vector store operations.

Covers:
- Vector indexing reconciliation lag and drift counts (VECTOR_INDEXING_LAG)
- Vector store write outcomes (VECTOR_WRITES_TOTAL)
- Taskiq vector reconciler row/run outcomes and stale-row lag
"""

from __future__ import annotations

import datetime as dt
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

    VECTOR_RECONCILE_ROWS_TOTAL = Counter(
        "ratatoskr_vector_reconcile_rows_total",
        "Rows observed by the Taskiq vector reconciler by outcome",
        ["outcome"],
        registry=REGISTRY,
    )

    VECTOR_RECONCILE_OLDEST_LAG_SECONDS = Gauge(
        "ratatoskr_vector_reconcile_oldest_lag_seconds",
        "Oldest stale row lag seen by the Taskiq vector reconciler",
        registry=REGISTRY,
    )

    VECTOR_RECONCILE_RUNS_TOTAL = Counter(
        "ratatoskr_vector_reconcile_runs_total",
        "Taskiq vector reconciler runs by terminal status",
        ["status"],
        registry=REGISTRY,
    )

else:
    VECTOR_INDEXING_LAG = None
    VECTOR_WRITES_TOTAL = None
    VECTOR_RECONCILE_ROWS_TOTAL = None
    VECTOR_RECONCILE_OLDEST_LAG_SECONDS = None
    VECTOR_RECONCILE_RUNS_TOTAL = None


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


def record_vector_reconcile_rows(
    *,
    scanned: int,
    requeued: int,
    skipped: int,
    failed: int,
) -> None:
    """Record per-run vector reconciler row outcomes."""
    if not PROMETHEUS_AVAILABLE or VECTOR_RECONCILE_ROWS_TOTAL is None:
        return
    for outcome, count in (
        ("scanned", scanned),
        ("requeued", requeued),
        ("skipped", skipped),
        ("failed", failed),
    ):
        VECTOR_RECONCILE_ROWS_TOTAL.labels(outcome=outcome).inc(max(0, count))


def set_vector_reconcile_oldest_lag_seconds(value: float | int | None) -> None:
    """Set the oldest stale-row lag observed by the vector reconciler."""
    if (
        not PROMETHEUS_AVAILABLE
        or VECTOR_RECONCILE_OLDEST_LAG_SECONDS is None
        or value is None
    ):
        return
    VECTOR_RECONCILE_OLDEST_LAG_SECONDS.set(max(0.0, float(value)))


def record_vector_reconcile_run(*, status: str) -> None:
    """Record one vector reconciler run completion status."""
    if not PROMETHEUS_AVAILABLE or VECTOR_RECONCILE_RUNS_TOTAL is None:
        return
    VECTOR_RECONCILE_RUNS_TOTAL.labels(status=status).inc()


def compute_vector_reconcile_oldest_lag_seconds(
    rows: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
) -> float:
    """Return oldest stale-row lag from rows selected by the Taskiq reconciler."""
    if not rows:
        return 0.0
    now = now or dt.datetime.now(dt.UTC)
    oldest = 0.0
    for row in rows:
        marker = row.get("last_indexed_at") or row.get("updated_at")
        if not isinstance(marker, dt.datetime):
            continue
        if marker.tzinfo is None:
            marker = marker.replace(tzinfo=dt.UTC)
        oldest = max(oldest, (now - marker).total_seconds())
    return max(0.0, oldest)
