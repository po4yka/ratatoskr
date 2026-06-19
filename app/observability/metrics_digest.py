"""Prometheus metrics for the Telegram channel digest subsystem."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

_DELIVERY_STATUSES = frozenset({"sent", "failed", "empty"})
_POST_ANALYSIS_STATUSES = frozenset({"ok", "llm_error", "skipped"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    DIGEST_DELIVERIES_TOTAL = Counter(
        "ratatoskr_digest_deliveries_total",
        "Telegram channel digest delivery outcomes",
        ["status"],
        registry=REGISTRY,
    )
    DIGEST_POSTS_ANALYZED_TOTAL = Counter(
        "ratatoskr_digest_posts_analyzed_total",
        "Telegram channel digest post-analysis outcomes",
        ["status"],
        registry=REGISTRY,
    )
    DIGEST_USERBOT_RECONNECTS_TOTAL = Counter(
        "ratatoskr_digest_userbot_reconnects_total",
        "Successful Telethon userbot session starts for digest ingestion",
        registry=REGISTRY,
    )
    DIGEST_CHANNEL_FETCH_ERRORS_TOTAL = Counter(
        "ratatoskr_digest_channel_fetch_errors_total",
        "Telegram channel digest fetch failures by bounded reason",
        ["reason"],
        registry=REGISTRY,
    )
    DIGEST_PIPELINE_DURATION_SECONDS = Histogram(
        "ratatoskr_digest_pipeline_duration_seconds",
        "End-to-end Telegram channel digest pipeline duration in seconds",
        ["digest_type", "status"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
        registry=REGISTRY,
    )
    DIGEST_ACTIVE_SUBSCRIPTION_USERS = Gauge(
        "ratatoskr_digest_active_subscription_users",
        "Users with active channel digest subscriptions seen by the scheduled run",
        registry=REGISTRY,
    )
else:
    DIGEST_DELIVERIES_TOTAL = None
    DIGEST_POSTS_ANALYZED_TOTAL = None
    DIGEST_USERBOT_RECONNECTS_TOTAL = None
    DIGEST_CHANNEL_FETCH_ERRORS_TOTAL = None
    DIGEST_PIPELINE_DURATION_SECONDS = None
    DIGEST_ACTIVE_SUBSCRIPTION_USERS = None


def record_digest_delivery(status: str) -> None:
    """Record one digest delivery outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    DIGEST_DELIVERIES_TOTAL.labels(status=_bucket(status, _DELIVERY_STATUSES)).inc()


def record_digest_posts_analyzed(status: str, *, count: int = 1) -> None:
    """Record post-analysis outcomes for one or more posts."""
    if not PROMETHEUS_AVAILABLE or count <= 0:
        return
    DIGEST_POSTS_ANALYZED_TOTAL.labels(
        status=_bucket(status, _POST_ANALYSIS_STATUSES),
    ).inc(count)


def record_digest_userbot_reconnect() -> None:
    """Record a successful userbot session start."""
    if not PROMETHEUS_AVAILABLE:
        return
    DIGEST_USERBOT_RECONNECTS_TOTAL.inc()


def record_digest_channel_fetch_error(reason: str) -> None:
    """Record one channel fetch error with a bounded reason label."""
    if not PROMETHEUS_AVAILABLE:
        return
    DIGEST_CHANNEL_FETCH_ERRORS_TOTAL.labels(reason=_metric_label(reason)).inc()


def record_digest_pipeline_duration(
    *,
    digest_type: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Record end-to-end digest pipeline latency."""
    if not PROMETHEUS_AVAILABLE:
        return
    DIGEST_PIPELINE_DURATION_SECONDS.labels(
        digest_type=_metric_label(digest_type),
        status=_bucket(status, _DELIVERY_STATUSES),
    ).observe(max(0.0, duration_seconds))


def set_digest_active_subscription_users(count: int) -> None:
    """Set the current scheduled-run active subscription user count."""
    if not PROMETHEUS_AVAILABLE:
        return
    DIGEST_ACTIVE_SUBSCRIPTION_USERS.set(max(0, count))


def _bucket(value: str, allowed: frozenset[str]) -> str:
    label = _metric_label(value)
    return label if label in allowed else "unknown"


__all__ = [
    "DIGEST_ACTIVE_SUBSCRIPTION_USERS",
    "DIGEST_CHANNEL_FETCH_ERRORS_TOTAL",
    "DIGEST_DELIVERIES_TOTAL",
    "DIGEST_PIPELINE_DURATION_SECONDS",
    "DIGEST_POSTS_ANALYZED_TOTAL",
    "DIGEST_USERBOT_RECONNECTS_TOTAL",
    "record_digest_channel_fetch_error",
    "record_digest_delivery",
    "record_digest_pipeline_duration",
    "record_digest_posts_analyzed",
    "record_digest_userbot_reconnect",
    "set_digest_active_subscription_users",
]
