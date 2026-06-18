"""Prometheus metrics for Twitter/X extraction and generic extraction pipeline.

Covers:
- Twitter/X article link resolution (TWITTER_ARTICLE_RESOLUTION, TWITTER_ARTICLE_RESOLUTION_LATENCY)
- Twitter/X article extraction by stage (TWITTER_ARTICLE_EXTRACTION)
- Generic normalized extraction attempts and failures (EXTRACTION_ATTEMPTS, EXTRACTION_FAILURES,
  EXTRACTION_STAGE_LATENCY)
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Histogram

    TWITTER_ARTICLE_RESOLUTION = Counter(
        "ratatoskr_twitter_article_resolution_total",
        "Twitter/X article resolution attempts",
        ["status", "reason"],
        registry=REGISTRY,
    )

    TWITTER_ARTICLE_RESOLUTION_LATENCY = Histogram(
        "ratatoskr_twitter_article_resolution_latency_seconds",
        "Twitter/X article resolution latency in seconds",
        ["status"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
        registry=REGISTRY,
    )

    TWITTER_ARTICLE_EXTRACTION = Counter(
        "ratatoskr_twitter_article_extraction_total",
        "Twitter/X article extraction attempts by stage",
        ["stage", "status", "reason"],
        registry=REGISTRY,
    )

    EXTRACTION_FAILURES = Counter(
        "ratatoskr_extraction_failures_total",
        "Normalized extraction failures",
        ["stage", "component", "reason_code", "retryable"],
        registry=REGISTRY,
    )

    EXTRACTION_ATTEMPTS = Counter(
        "ratatoskr_extraction_attempts_total",
        "Extraction attempts by stage/component/outcome",
        ["stage", "component", "outcome"],
        registry=REGISTRY,
    )

    EXTRACTION_STAGE_LATENCY = Histogram(
        "ratatoskr_extraction_stage_latency_seconds",
        "Extraction stage latency in seconds",
        ["stage", "component", "outcome"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
        registry=REGISTRY,
    )

else:
    TWITTER_ARTICLE_RESOLUTION = None
    TWITTER_ARTICLE_RESOLUTION_LATENCY = None
    TWITTER_ARTICLE_EXTRACTION = None
    EXTRACTION_FAILURES = None
    EXTRACTION_ATTEMPTS = None
    EXTRACTION_STAGE_LATENCY = None


def record_twitter_article_resolution(
    status: str,
    reason: str,
    latency_seconds: float | None = None,
) -> None:
    """Record a Twitter/X article link resolution attempt."""
    if not PROMETHEUS_AVAILABLE:
        return

    TWITTER_ARTICLE_RESOLUTION.labels(status=status, reason=reason).inc()
    outcome = "success" if status == "hit" else "failure"
    EXTRACTION_ATTEMPTS.labels(
        stage="resolution", component="twitter_resolver", outcome=outcome
    ).inc()
    if status != "hit":
        retryable = "true" if status == "error" else "false"
        EXTRACTION_FAILURES.labels(
            stage="resolution",
            component="twitter_resolver",
            reason_code=reason.upper(),
            retryable=retryable,
        ).inc()
    if latency_seconds is not None:
        TWITTER_ARTICLE_RESOLUTION_LATENCY.labels(status=status).observe(latency_seconds)
        EXTRACTION_STAGE_LATENCY.labels(
            stage="resolution",
            component="twitter_resolver",
            outcome=outcome,
        ).observe(latency_seconds)


def record_twitter_article_extraction(stage: str, status: str, reason: str) -> None:
    """Record a Twitter/X article extraction attempt."""
    if not PROMETHEUS_AVAILABLE:
        return

    TWITTER_ARTICLE_EXTRACTION.labels(stage=stage, status=status, reason=reason).inc()
    component = f"twitter_{stage}"
    outcome = "success" if status == "success" else "failure"
    EXTRACTION_ATTEMPTS.labels(stage="extraction", component=component, outcome=outcome).inc()
    if outcome == "failure":
        EXTRACTION_FAILURES.labels(
            stage="extraction",
            component=component,
            reason_code=reason.upper(),
            retryable="true",
        ).inc()


def record_extraction_attempt(
    *,
    stage: str,
    component: str,
    outcome: str,
    latency_seconds: float | None = None,
) -> None:
    """Record normalized extraction attempts and optional latency."""
    if not PROMETHEUS_AVAILABLE:
        return

    EXTRACTION_ATTEMPTS.labels(stage=stage, component=component, outcome=outcome).inc()
    if latency_seconds is not None:
        EXTRACTION_STAGE_LATENCY.labels(
            stage=stage,
            component=component,
            outcome=outcome,
        ).observe(latency_seconds)


def record_extraction_failure(
    *,
    stage: str,
    component: str,
    reason_code: str,
    retryable: bool,
) -> None:
    """Record normalized extraction failures."""
    if not PROMETHEUS_AVAILABLE:
        return

    EXTRACTION_FAILURES.labels(
        stage=stage,
        component=component,
        reason_code=reason_code,
        retryable="true" if retryable else "false",
    ).inc()
