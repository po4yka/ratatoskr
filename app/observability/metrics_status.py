"""Bounded Prometheus metrics for public status checks."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

_COMPONENTS = frozenset(
    {
        "api",
        "web_application",
        "telegram_bot",
        "postgresql",
        "redis",
        "vector_search",
        "extraction",
        "ai_summarization",
        "taskiq_worker",
        "scheduler",
        "vector_reconciliation",
        "postgresql_backup",
        "github_repository_backups",
        "chatgpt_backup",
        "claude_backup",
    }
)
_STATUSES = frozenset({"operational", "degraded", "outage", "unknown", "disabled"})
_STATE_VALUES = {
    "unknown": 0,
    "operational": 1,
    "degraded": 2,
    "outage": 3,
    "disabled": 4,
}

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    STATUS_CHECKS_TOTAL = Counter(
        "ratatoskr_status_checks_total",
        "Public status component checks by bounded component and result.",
        ["component", "status"],
        registry=REGISTRY,
    )
    STATUS_CHECK_DURATION_SECONDS = Histogram(
        "ratatoskr_status_check_duration_seconds",
        "Public status component check duration.",
        ["component"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        registry=REGISTRY,
    )
    STATUS_COMPONENT_STATE = Gauge(
        "ratatoskr_status_component_state",
        "Current public status component state: 0 unknown, 1 operational, 2 degraded, 3 outage, 4 disabled.",
        ["component"],
        multiprocess_mode="livemostrecent",
        registry=REGISTRY,
    )
else:
    STATUS_CHECKS_TOTAL = None
    STATUS_CHECK_DURATION_SECONDS = None
    STATUS_COMPONENT_STATE = None


def record_status_check(component: str, status: str, duration_seconds: float) -> None:
    """Record one component check with fixed-cardinality labels."""
    if not PROMETHEUS_AVAILABLE:
        return
    component_label = component if component in _COMPONENTS else "other"
    status_label = status if status in _STATUSES else "unknown"
    STATUS_CHECKS_TOTAL.labels(component=component_label, status=status_label).inc()
    STATUS_CHECK_DURATION_SECONDS.labels(component=component_label).observe(
        max(0.0, duration_seconds)
    )
    STATUS_COMPONENT_STATE.labels(component=component_label).set(_STATE_VALUES[status_label])
