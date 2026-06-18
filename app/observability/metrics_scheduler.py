"""Prometheus metrics for APScheduler / background job queues.

Covers:
- Chronic scheduler job failure counter (SCHEDULER_JOB_CHRONIC_FAILURES)
- Named background queue depth gauge (SCHEDULER_QUEUE_DEPTH)
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge

    SCHEDULER_JOB_CHRONIC_FAILURES = Counter(
        "ratatoskr_scheduler_job_chronic_failures_total",
        "Scheduler jobs that have failed 3+ consecutive ticks",
        ["job_id"],
        registry=REGISTRY,
    )

    # Snapshot depth of any scheduler or background queue at reporting time.
    # Label "queue" distinguishes multiple queues (e.g. "url_processor",
    # "taskiq", "rss").
    SCHEDULER_QUEUE_DEPTH = Gauge(
        "ratatoskr_scheduler_queue_depth",
        "Current depth of a named background job queue",
        ["queue"],
        registry=REGISTRY,
    )

else:
    SCHEDULER_JOB_CHRONIC_FAILURES = None
    SCHEDULER_QUEUE_DEPTH = None


def record_scheduler_chronic_failure(job_id: str) -> None:
    """Increment the chronic-failure counter for a scheduler job."""
    if not PROMETHEUS_AVAILABLE:
        return
    SCHEDULER_JOB_CHRONIC_FAILURES.labels(job_id=job_id).inc()


def set_scheduler_queue_depth(queue: str, depth: int) -> None:
    """Set the current depth of a named background job queue.

    Args:
        queue: Queue name label (e.g. "url_processor", "taskiq", "rss").
        depth: Current number of waiting jobs.  Negative values are silently
            ignored.
    """
    if not PROMETHEUS_AVAILABLE or SCHEDULER_QUEUE_DEPTH is None:
        return
    if depth < 0:
        return
    SCHEDULER_QUEUE_DEPTH.labels(queue=_metric_label(queue)).set(depth)
