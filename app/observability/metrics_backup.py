"""Fixed-cardinality metrics for backup runs and item outcomes."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

_BACKUPS = frozenset({"github_repositories", "chatgpt", "claude"})
_RUN_OUTCOMES = frozenset(
    {"success", "partial", "error", "auth_required", "unverified", "skipped", "disabled"}
)
_ITEM_RESULTS = frozenset({"ok", "failed", "skipped"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge

    BACKUP_RUNS_TOTAL = Counter(
        "ratatoskr_backup_runs_total",
        "Backup runs by fixed subsystem and truthful terminal outcome.",
        ["backup", "outcome"],
        registry=REGISTRY,
    )
    BACKUP_ITEMS = Gauge(
        "ratatoskr_backup_items",
        "Items observed in the latest backup run by fixed subsystem and result.",
        ["backup", "result"],
        multiprocess_mode="livemostrecent",
        registry=REGISTRY,
    )
else:
    BACKUP_RUNS_TOTAL = None
    BACKUP_ITEMS = None


def _bounded(value: str, allowed: frozenset[str]) -> str:
    label = _metric_label(value)
    return label if label in allowed else "unknown"


def record_backup_run(backup: str, outcome: str) -> None:
    """Record a terminal run outcome without user/repository labels."""
    if not PROMETHEUS_AVAILABLE:
        return
    BACKUP_RUNS_TOTAL.labels(
        backup=_bounded(backup, _BACKUPS),
        outcome=_bounded(outcome, _RUN_OUTCOMES),
    ).inc()


def set_backup_items(backup: str, *, ok: int, failed: int, skipped: int) -> None:
    """Publish the latest aggregate item counts for a backup subsystem."""
    if not PROMETHEUS_AVAILABLE:
        return
    backup_label = _bounded(backup, _BACKUPS)
    for result, count in (("ok", ok), ("failed", failed), ("skipped", skipped)):
        BACKUP_ITEMS.labels(
            backup=backup_label,
            result=_bounded(result, _ITEM_RESULTS),
        ).set(max(0, count))


__all__ = ["BACKUP_ITEMS", "BACKUP_RUNS_TOTAL", "record_backup_run", "set_backup_items"]
