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
    from prometheus_client import Counter, Gauge

    SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL = Counter(
        "ratatoskr_source_artifact_invariant_violations_total",
        "Attempts to complete a URL request without a durable normalized source artifact",
        ["stage"],
        registry=REGISTRY,
    )
    SOURCE_CONTENT_RECONCILE_ROWS_TOTAL = Counter(
        "ratatoskr_source_content_reconcile_rows_total",
        "Reader source-content reconciliation rows by outcome",
        ["outcome"],
        registry=REGISTRY,
    )
    SOURCE_CONTENT_RECONCILE_RUNS_TOTAL = Counter(
        "ratatoskr_source_content_reconcile_runs_total",
        "Reader source-content reconciliation runs by terminal status",
        ["status"],
        registry=REGISTRY,
    )
    SOURCE_CONTENT_MISSING = Gauge(
        "ratatoskr_source_content_missing",
        "Completed summaries missing normalized Reader source content in the latest scan",
        multiprocess_mode="mostrecent",
        registry=REGISTRY,
    )
    SOURCE_CONTENT_OLDEST_MISSING_AGE_SECONDS = Gauge(
        "ratatoskr_source_content_oldest_missing_age_seconds",
        "Age of the oldest completed summary missing normalized Reader source content",
        multiprocess_mode="mostrecent",
        registry=REGISTRY,
    )
else:
    SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL = None
    SOURCE_CONTENT_RECONCILE_ROWS_TOTAL = None
    SOURCE_CONTENT_RECONCILE_RUNS_TOTAL = None
    SOURCE_CONTENT_MISSING = None
    SOURCE_CONTENT_OLDEST_MISSING_AGE_SECONDS = None


def record_source_artifact_invariant_violation(*, stage: str) -> None:
    """Record an invariant rejection with a bounded-cardinality stage label."""
    if not PROMETHEUS_AVAILABLE or SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL is None:
        return
    label = stage if stage in _KNOWN_STAGES else "other"
    SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL.labels(stage=label).inc()


def record_source_content_reconcile_rows(**outcomes: int) -> None:
    """Record bounded reconciliation outcomes."""
    if not PROMETHEUS_AVAILABLE or SOURCE_CONTENT_RECONCILE_ROWS_TOTAL is None:
        return
    for outcome in ("scanned", "local_repaired", "reextracted", "skipped", "failed"):
        SOURCE_CONTENT_RECONCILE_ROWS_TOTAL.labels(outcome=outcome).inc(
            max(0, int(outcomes.get(outcome, 0)))
        )


def record_source_content_reconcile_run(*, status: str) -> None:
    if not PROMETHEUS_AVAILABLE or SOURCE_CONTENT_RECONCILE_RUNS_TOTAL is None:
        return
    label = status if status in {"success", "error", "disabled", "lock_held"} else "other"
    SOURCE_CONTENT_RECONCILE_RUNS_TOTAL.labels(status=label).inc()


def set_source_content_missing(value: int) -> None:
    if not PROMETHEUS_AVAILABLE or SOURCE_CONTENT_MISSING is None:
        return
    SOURCE_CONTENT_MISSING.set(max(0, value))


def set_source_content_oldest_missing_age_seconds(value: float) -> None:
    if not PROMETHEUS_AVAILABLE or SOURCE_CONTENT_OLDEST_MISSING_AGE_SECONDS is None:
        return
    SOURCE_CONTENT_OLDEST_MISSING_AGE_SECONDS.set(max(0, value))
