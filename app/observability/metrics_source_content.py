"""Prometheus signals for durable Reader source-content invariants."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

_KNOWN_STAGES = frozenset(
    {
        "graph_persist",
        "request_completion",
        "job_completion",
        "synchronous_completion",
    }
)

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter

    SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL = Counter(
        "ratatoskr_source_artifact_invariant_violations_total",
        "Attempts to complete a URL request without a durable normalized source artifact",
        ["stage"],
        registry=REGISTRY,
    )
else:
    SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL = None


def record_source_artifact_invariant_violation(*, stage: str) -> None:
    """Record an invariant rejection with a bounded-cardinality stage label."""
    if not PROMETHEUS_AVAILABLE or SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL is None:
        return
    label = stage if stage in _KNOWN_STAGES else "other"
    SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL.labels(stage=label).inc()
