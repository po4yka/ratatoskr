"""Prometheus metrics for authentication and session security."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

_TOKEN_FAMILY_DECISIONS = frozenset({"rotate", "reject", "revoke_family", "unknown"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter

    TOKEN_FAMILY_DECISIONS_TOTAL = Counter(
        "ratatoskr_token_family_decisions_total",
        "Refresh-token family policy decisions",
        ["decision"],
        registry=REGISTRY,
    )
else:
    TOKEN_FAMILY_DECISIONS_TOTAL = None


def record_token_family_decision(decision: str) -> None:
    """Record one refresh-token family policy decision."""
    if not PROMETHEUS_AVAILABLE:
        return
    label = _metric_label(decision)
    TOKEN_FAMILY_DECISIONS_TOTAL.labels(
        decision=label if label in _TOKEN_FAMILY_DECISIONS else "unknown"
    ).inc()


__all__ = [
    "TOKEN_FAMILY_DECISIONS_TOTAL",
    "record_token_family_decision",
]
