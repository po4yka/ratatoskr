"""Bounded RED metrics for generic Taskiq execution."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

_TASKS = frozenset(
    {
        "ratatoskr.ai_backup.sync",
        "ratatoskr.data.purge",
        "ratatoskr.digest.run",
        "ratatoskr.git_backup.sync",
        "ratatoskr.github.sync_stars",
        "ratatoskr.import.process_bookmarks",
        "ratatoskr.langgraph.prune",
        "ratatoskr.rss.poll",
        "ratatoskr.topic_change_watch.run",
        "ratatoskr.url.process",
        "ratatoskr.vector.reconcile",
        "ratatoskr.x.sync_bookmarks",
        "ratatoskr.x.sync_wiki",
    }
)

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    TASKIQ_EXECUTIONS_TOTAL = Counter(
        "ratatoskr_taskiq_executions_total",
        "Taskiq task executions by registered task and outcome.",
        ["task", "outcome"],
        registry=REGISTRY,
    )
    TASKIQ_EXECUTION_DURATION_SECONDS = Histogram(
        "ratatoskr_taskiq_execution_duration_seconds",
        "Taskiq task execution duration by registered task.",
        ["task"],
        buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300, 900, 3600),
        registry=REGISTRY,
    )
    TASKIQ_IN_FLIGHT = Gauge(
        "ratatoskr_taskiq_in_flight",
        "Taskiq task executions currently in flight.",
        ["task"],
        multiprocess_mode="livesum",
        registry=REGISTRY,
    )
else:
    TASKIQ_EXECUTIONS_TOTAL = None
    TASKIQ_EXECUTION_DURATION_SECONDS = None
    TASKIQ_IN_FLIGHT = None


def bucket_taskiq_task(task_name: str) -> str:
    return task_name if task_name in _TASKS else "other"


def change_taskiq_in_flight(task_name: str, amount: int) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    TASKIQ_IN_FLIGHT.labels(task=bucket_taskiq_task(task_name)).inc(amount)


def record_taskiq_execution(
    *, task_name: str, is_error: bool, duration_seconds: float
) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    task = bucket_taskiq_task(task_name)
    outcome = "error" if is_error else "success"
    TASKIQ_EXECUTIONS_TOTAL.labels(task=task, outcome=outcome).inc()
    TASKIQ_EXECUTION_DURATION_SECONDS.labels(task=task).observe(max(0.0, duration_seconds))
