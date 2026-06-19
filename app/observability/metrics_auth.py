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
    RATE_LIMIT_HITS_TOTAL = Counter(
        "ratatoskr_rate_limit_hits_total",
        "API rate-limit rejections by bucket",
        ["bucket"],
        registry=REGISTRY,
    )
else:
    TOKEN_FAMILY_DECISIONS_TOTAL = None
    RATE_LIMIT_HITS_TOTAL = None


def record_token_family_decision(decision: str) -> None:
    """Record one refresh-token family policy decision."""
    if not PROMETHEUS_AVAILABLE:
        return
    label = _metric_label(decision)
    TOKEN_FAMILY_DECISIONS_TOTAL.labels(
        decision=label if label in _TOKEN_FAMILY_DECISIONS else "unknown"
    ).inc()


def record_rate_limit_hit(bucket: str) -> None:
    """Record one API rate-limit rejection."""
    if not PROMETHEUS_AVAILABLE:
        return
    RATE_LIMIT_HITS_TOTAL.labels(bucket=_metric_label(bucket)).inc()


__all__ = [
    "RATE_LIMIT_HITS_TOTAL",
    "TOKEN_FAMILY_DECISIONS_TOTAL",
    "record_rate_limit_hit",
    "record_token_family_decision",
]
