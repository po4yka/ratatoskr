"""Prometheus metrics for social provider integrations.

Covers:
- Content fetch attempts by provider/status/auth tier (SOCIAL_FETCH_TOTAL)
- OAuth token refresh attempts (SOCIAL_TOKEN_REFRESH_TOTAL)
- Rate-limit responses (SOCIAL_RATE_LIMIT_TOTAL)
- Connection status observations (SOCIAL_CONNECTION_STATUS_TOTAL)
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter

    SOCIAL_FETCH_TOTAL = Counter(
        "ratatoskr_social_fetch_total",
        "Social provider content fetch attempts by provider, status, and auth tier",
        ["provider", "status", "auth_tier"],
        registry=REGISTRY,
    )
    SOCIAL_TOKEN_REFRESH_TOTAL = Counter(
        "ratatoskr_social_token_refresh_total",
        "Social provider token refresh attempts by provider and status",
        ["provider", "status"],
        registry=REGISTRY,
    )
    SOCIAL_RATE_LIMIT_TOTAL = Counter(
        "ratatoskr_social_rate_limit_total",
        "Social provider rate-limit responses by provider",
        ["provider"],
        registry=REGISTRY,
    )
    SOCIAL_CONNECTION_STATUS_TOTAL = Counter(
        "ratatoskr_social_connection_status_total",
        "Social connection status observations by provider and status",
        ["provider", "status"],
        registry=REGISTRY,
    )

else:
    SOCIAL_FETCH_TOTAL = None
    SOCIAL_TOKEN_REFRESH_TOTAL = None
    SOCIAL_RATE_LIMIT_TOTAL = None
    SOCIAL_CONNECTION_STATUS_TOTAL = None


def record_social_fetch(*, provider: str, status: str, auth_tier: str) -> None:
    """Record a social provider content fetch attempt."""
    if not PROMETHEUS_AVAILABLE:
        return
    SOCIAL_FETCH_TOTAL.labels(
        provider=_metric_label(provider),
        status=_metric_label(status),
        auth_tier=_metric_label(auth_tier),
    ).inc()


def record_social_token_refresh(*, provider: str, status: str) -> None:
    """Record a social provider OAuth token refresh attempt."""
    if not PROMETHEUS_AVAILABLE:
        return
    SOCIAL_TOKEN_REFRESH_TOTAL.labels(
        provider=_metric_label(provider),
        status=_metric_label(status),
    ).inc()


def record_social_rate_limit(*, provider: str) -> None:
    """Record a social provider rate-limit response."""
    if not PROMETHEUS_AVAILABLE:
        return
    SOCIAL_RATE_LIMIT_TOTAL.labels(provider=_metric_label(provider)).inc()


def record_social_connection_status(*, provider: str, status: str) -> None:
    """Record an observed social connection status."""
    if not PROMETHEUS_AVAILABLE:
        return
    SOCIAL_CONNECTION_STATUS_TOTAL.labels(
        provider=_metric_label(provider),
        status=_metric_label(status),
    ).inc()
