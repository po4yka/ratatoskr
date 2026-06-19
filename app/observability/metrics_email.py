"""Prometheus metrics for outbound email delivery."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

_EMAIL_OUTCOMES = frozenset({"sent", "failed", "skipped", "disabled"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter

    EMAIL_DELIVERIES_TOTAL = Counter(
        "ratatoskr_email_deliveries_total",
        "Outbound email delivery attempts by outcome",
        ["outcome"],
        registry=REGISTRY,
    )
else:
    EMAIL_DELIVERIES_TOTAL = None


def record_email_delivery(outcome: str) -> None:
    """Record one outbound email delivery outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    label = _metric_label(outcome)
    EMAIL_DELIVERIES_TOTAL.labels(outcome=label if label in _EMAIL_OUTCOMES else "unknown").inc()


__all__ = ["EMAIL_DELIVERIES_TOTAL", "record_email_delivery"]
