"""Prometheus metrics for database operations and admin diagnostics.

Covers:
- DB query latency histogram (DB_QUERY_LATENCY)
- Active DB connection gauge (DB_CONNECTIONS)
- Owner diagnostics API request counter (ADMIN_DIAGNOSTICS_REQUESTS)
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    DB_QUERY_LATENCY = Histogram(
        "ratatoskr_db_query_latency_seconds",
        "Database query latency in seconds",
        ["operation"],
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
        registry=REGISTRY,
    )

    DB_CONNECTIONS = Gauge(
        "ratatoskr_db_connections_active",
        "Number of active database connections",
        multiprocess_mode="livesum",
        registry=REGISTRY,
    )

    ADMIN_DIAGNOSTICS_REQUESTS = Counter(
        "ratatoskr_admin_diagnostics_requests_total",
        "Owner diagnostics API requests by outcome",
        ["status"],
        registry=REGISTRY,
    )

else:
    DB_QUERY_LATENCY = None
    DB_CONNECTIONS = None
    ADMIN_DIAGNOSTICS_REQUESTS = None


def record_db_query(operation: str, latency_seconds: float) -> None:
    """Record a database query metric.

    Args:
        operation: Query operation type (select, insert, update, delete)
        latency_seconds: Query latency in seconds
    """
    if not PROMETHEUS_AVAILABLE:
        return

    DB_QUERY_LATENCY.labels(operation=operation).observe(latency_seconds)


def set_db_connections(count: int) -> None:
    """Set the number of active database connections.

    Args:
        count: Number of active connections
    """
    if not PROMETHEUS_AVAILABLE:
        return

    DB_CONNECTIONS.set(count)


def record_admin_diagnostics_request(status: str) -> None:
    """Record an owner diagnostics API request outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    ADMIN_DIAGNOSTICS_REQUESTS.labels(status=status).inc()
