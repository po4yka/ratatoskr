"""Prometheus metrics for the scraper chain (Firecrawl + multi-provider chain).

Covers:
- Firecrawl API requests and latency
- Per-provider scraper attempt counters and latency
- End-to-end chain invocation latency and failure breakdown
- Per-provider chain-level attempt/success/duration counters
"""

from __future__ import annotations

import time

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    # Firecrawl metrics
    FIRECRAWL_REQUESTS = Counter(
        "ratatoskr_firecrawl_requests_total",
        "Total Firecrawl API requests",
        ["status", "endpoint"],
        registry=REGISTRY,
    )

    FIRECRAWL_LATENCY = Histogram(
        "ratatoskr_firecrawl_latency_seconds",
        "Firecrawl API latency in seconds",
        ["endpoint"],
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
        registry=REGISTRY,
    )

    # Per-provider attempt counter (status: success | error | timeout | skipped)
    # and per-provider latency histogram.
    SCRAPER_ATTEMPTS_TOTAL = Counter(
        "ratatoskr_scraper_attempts_total",
        "Total scraper provider attempts by outcome",
        ["provider", "status"],
        registry=REGISTRY,
    )
    SCRAPER_ATTEMPT_LATENCY_SECONDS = Histogram(
        "ratatoskr_scraper_attempt_latency_seconds",
        "Latency of a single scraper provider attempt in seconds",
        ["provider"],
        buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
        registry=REGISTRY,
    )

    # End-to-end wall time for a full chain invocation.
    SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS = Histogram(
        "ratatoskr_scraper_chain_total_latency_seconds",
        "Total wall time for one scraper chain invocation in seconds",
        ["mode", "outcome"],
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 60.0, 90.0, 120.0],
        registry=REGISTRY,
    )
    SCRAPER_CHAIN_LAST_RESULT_TIMESTAMP_SECONDS = Gauge(
        "ratatoskr_scraper_chain_last_result_timestamp_seconds",
        "Unix timestamp of the latest scraper-chain success or runtime failure",
        ["outcome"],
        registry=REGISTRY,
    )

    # Per-provider failure breakdown by reason.
    SCRAPER_CHAIN_FAILURES_TOTAL = Counter(
        "ratatoskr_scraper_chain_failures_total",
        "Scraper chain provider failures by provider and failure reason",
        ["provider", "reason"],
        registry=REGISTRY,
    )

    # Chain-level attempt/success/duration per provider (one observation per
    # chain invocation, not per tier attempt).
    SCRAPER_CHAIN_ATTEMPTS_TOTAL = Counter(
        "ratatoskr_scraper_chain_attempts_total",
        "Total scraper chain invocations attempted per provider",
        ["provider"],
        registry=REGISTRY,
    )
    SCRAPER_CHAIN_SUCCESSES_TOTAL = Counter(
        "ratatoskr_scraper_chain_successes_total",
        "Total scraper chain invocations that succeeded per provider",
        ["provider"],
        registry=REGISTRY,
    )
    SCRAPER_CHAIN_DURATION_SECONDS = Histogram(
        "ratatoskr_scraper_chain_duration_seconds",
        "Duration of a scraper chain invocation per provider in seconds",
        ["provider"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
        registry=REGISTRY,
    )

else:
    FIRECRAWL_REQUESTS = None
    FIRECRAWL_LATENCY = None
    SCRAPER_ATTEMPTS_TOTAL = None
    SCRAPER_ATTEMPT_LATENCY_SECONDS = None
    SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS = None
    SCRAPER_CHAIN_LAST_RESULT_TIMESTAMP_SECONDS = None
    SCRAPER_CHAIN_FAILURES_TOTAL = None
    SCRAPER_CHAIN_ATTEMPTS_TOTAL = None
    SCRAPER_CHAIN_SUCCESSES_TOTAL = None
    SCRAPER_CHAIN_DURATION_SECONDS = None


def record_firecrawl_request(
    status: str,
    endpoint: str = "scrape",
    latency_seconds: float | None = None,
) -> None:
    """Record a Firecrawl API request metric.

    Args:
        status: Request status (success, error, timeout)
        endpoint: API endpoint (scrape, search, crawl)
        latency_seconds: Optional latency in seconds
    """
    if not PROMETHEUS_AVAILABLE:
        return

    FIRECRAWL_REQUESTS.labels(status=status, endpoint=endpoint).inc()

    if latency_seconds is not None:
        FIRECRAWL_LATENCY.labels(endpoint=endpoint).observe(latency_seconds)


def record_scraper_attempt(*, provider: str, status: str) -> None:
    """Record a single scraper-chain provider attempt.

    Args:
        provider: One of the chain providers (``scrapling``, ``crawl4ai``,
            ``firecrawl``, ``defuddle``, ``playwright``, ``crawlee``,
            ``direct_html``, ``scrapegraph_ai``).
        status: ``success`` | ``error`` | ``timeout`` | ``skipped``.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    SCRAPER_ATTEMPTS_TOTAL.labels(provider=provider, status=status).inc()


def record_scraper_attempt_latency(*, provider: str, latency_seconds: float) -> None:
    """Record per-attempt scraper provider latency."""
    if not PROMETHEUS_AVAILABLE:
        return
    if latency_seconds < 0:
        return
    SCRAPER_ATTEMPT_LATENCY_SECONDS.labels(provider=provider).observe(latency_seconds)


def record_scraper_chain_total_latency(
    *,
    mode: str,
    outcome: str,
    total_latency_seconds: float,
) -> None:
    """Record end-to-end wall time of one scraper chain invocation.

    Args:
        mode: ``serial`` (legacy ordered fallback) or ``tiered_race`` (the
            free/paid/browser tier-based race introduced in Tier 1 of the
            speedup plan).
        outcome: ``success`` | ``empty`` | ``dns_failed`` | ``ssrf_blocked``.
        total_latency_seconds: Wall time from the chain entry to the
            returned ``FirecrawlResult`` (success or final error).
    """
    if not PROMETHEUS_AVAILABLE or SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS is None:
        return
    if total_latency_seconds < 0:
        return
    SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS.labels(mode=mode, outcome=outcome).observe(
        total_latency_seconds
    )
    health_outcome = {
        "success": "success",
        "empty": "failure",
        "dns_failed": "failure",
    }.get(outcome)
    if health_outcome is not None and SCRAPER_CHAIN_LAST_RESULT_TIMESTAMP_SECONDS is not None:
        SCRAPER_CHAIN_LAST_RESULT_TIMESTAMP_SECONDS.labels(outcome=health_outcome).set(time.time())


def record_scraper_chain_failure(*, provider: str, reason: str) -> None:
    """Record a scraper chain provider failure with a specific reason.

    Args:
        provider: Provider name (``scrapling``, ``crawl4ai``, etc.)
        reason: One of ``empty``, ``error``, ``error_page``, ``too_short``,
            ``low_value``.
    """
    if not PROMETHEUS_AVAILABLE or SCRAPER_CHAIN_FAILURES_TOTAL is None:
        return
    SCRAPER_CHAIN_FAILURES_TOTAL.labels(provider=provider, reason=reason).inc()


def record_scraper_chain_attempt(*, provider: str) -> None:
    """Increment the per-provider chain-level attempt counter.

    Called once per provider invocation regardless of outcome, before the
    provider is awaited.  Pair with :func:`record_scraper_chain_success` and
    :func:`record_scraper_chain_failure` to derive a per-provider success rate.

    Args:
        provider: Provider name (e.g. ``scrapling``, ``firecrawl``,
            ``playwright``).
    """
    if not PROMETHEUS_AVAILABLE or SCRAPER_CHAIN_ATTEMPTS_TOTAL is None:
        return
    SCRAPER_CHAIN_ATTEMPTS_TOTAL.labels(provider=provider).inc()


def record_scraper_chain_success(*, provider: str) -> None:
    """Increment the per-provider chain-level success counter.

    Called after a provider returns content that passes all chain-side quality
    gates (non-empty, not an error page, not too short, not low-value).

    Args:
        provider: Provider name that produced usable content.
    """
    if not PROMETHEUS_AVAILABLE or SCRAPER_CHAIN_SUCCESSES_TOTAL is None:
        return
    SCRAPER_CHAIN_SUCCESSES_TOTAL.labels(provider=provider).inc()


def record_scraper_chain_duration(*, provider: str, latency_seconds: float) -> None:
    """Observe per-provider scraper chain attempt wall time.

    Negative values are silently dropped.

    Args:
        provider: Provider name whose attempt duration is being recorded.
        latency_seconds: Wall-clock seconds from attempt start to outcome.
    """
    if not PROMETHEUS_AVAILABLE or SCRAPER_CHAIN_DURATION_SECONDS is None:
        return
    if latency_seconds < 0:
        return
    SCRAPER_CHAIN_DURATION_SECONDS.labels(provider=provider).observe(latency_seconds)
