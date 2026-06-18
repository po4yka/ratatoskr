"""Prometheus metrics for multi-source aggregation workloads.

Covers:
- Per-item extraction outcomes (AGGREGATION_EXTRACTION)
- Bundle-level outcomes and latency (AGGREGATION_BUNDLES, AGGREGATION_BUNDLE_LATENCY)
- Synthesis coverage ratio and used-source counts (AGGREGATION_SYNTHESIS_COVERAGE,
  AGGREGATION_USED_SOURCES)
- Synthesis cost (AGGREGATION_COST_USD)
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _bucket_platform

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Histogram

    AGGREGATION_EXTRACTION = Counter(
        "ratatoskr_aggregation_extraction_total",
        "Aggregation extraction outcomes by source kind, platform, fallback tier, and media type",
        ["source_kind", "platform", "outcome", "fallback_tier", "media_type"],
        registry=REGISTRY,
    )

    AGGREGATION_BUNDLES = Counter(
        "ratatoskr_aggregation_bundles_total",
        "Aggregation bundle outcomes by entrypoint and partial-success state",
        ["entrypoint", "status", "partial_success", "bundle_profile"],
        registry=REGISTRY,
    )

    AGGREGATION_BUNDLE_LATENCY = Histogram(
        "ratatoskr_aggregation_bundle_latency_seconds",
        "End-to-end aggregation bundle latency in seconds",
        ["entrypoint", "status", "bundle_profile"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
        registry=REGISTRY,
    )

    AGGREGATION_SYNTHESIS_COVERAGE = Histogram(
        "ratatoskr_aggregation_synthesis_coverage_ratio",
        "Share of extracted sources used in final synthesis",
        ["source_type", "bundle_profile", "status"],
        buckets=[0.0, 0.25, 0.5, 0.75, 1.0],
        registry=REGISTRY,
    )

    AGGREGATION_USED_SOURCES = Histogram(
        "ratatoskr_aggregation_used_sources",
        "Count of sources contributing to final aggregation output",
        ["source_type", "bundle_profile", "status"],
        buckets=[1, 2, 3, 5, 8, 13, 21, 34],
        registry=REGISTRY,
    )

    AGGREGATION_COST_USD = Counter(
        "ratatoskr_aggregation_cost_usd_total",
        "Total synthesis cost in USD for aggregation workloads",
        ["source_type", "bundle_profile", "status"],
        registry=REGISTRY,
    )

else:
    AGGREGATION_EXTRACTION = None
    AGGREGATION_BUNDLES = None
    AGGREGATION_BUNDLE_LATENCY = None
    AGGREGATION_SYNTHESIS_COVERAGE = None
    AGGREGATION_USED_SOURCES = None
    AGGREGATION_COST_USD = None


def record_aggregation_extraction(
    *,
    source_kind: str,
    platform: str,
    outcome: str,
    fallback_tier: str,
    media_type: str,
) -> None:
    """Record one item-level aggregation extraction outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    AGGREGATION_EXTRACTION.labels(
        source_kind=source_kind,
        platform=_bucket_platform(platform),
        outcome=outcome,
        fallback_tier=fallback_tier,
        media_type=media_type,
    ).inc()


def record_aggregation_bundle(
    *,
    entrypoint: str,
    status: str,
    partial_success: bool,
    bundle_profile: str,
    latency_seconds: float | None = None,
) -> None:
    """Record bundle-level outcome and optional end-to-end latency."""
    if not PROMETHEUS_AVAILABLE:
        return
    AGGREGATION_BUNDLES.labels(
        entrypoint=entrypoint,
        status=status,
        partial_success="true" if partial_success else "false",
        bundle_profile=bundle_profile,
    ).inc()
    if latency_seconds is not None:
        AGGREGATION_BUNDLE_LATENCY.labels(
            entrypoint=entrypoint,
            status=status,
            bundle_profile=bundle_profile,
        ).observe(latency_seconds)


def record_aggregation_synthesis(
    *,
    source_type: str,
    bundle_profile: str,
    status: str,
    used_source_count: int,
    coverage_ratio: float,
    cost_usd: float = 0.0,
) -> None:
    """Record synthesis coverage and used-source counts for aggregation output."""
    if not PROMETHEUS_AVAILABLE:
        return
    AGGREGATION_SYNTHESIS_COVERAGE.labels(
        source_type=source_type,
        bundle_profile=bundle_profile,
        status=status,
    ).observe(max(0.0, min(1.0, coverage_ratio)))
    AGGREGATION_USED_SOURCES.labels(
        source_type=source_type,
        bundle_profile=bundle_profile,
        status=status,
    ).observe(max(0, used_source_count))
    if cost_usd > 0:
        AGGREGATION_COST_USD.labels(
            source_type=source_type,
            bundle_profile=bundle_profile,
            status=status,
        ).inc(cost_usd)
