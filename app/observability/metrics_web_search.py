"""Prometheus metrics for web-search enrichment decisions."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

_KNOWN_DECISIONS = frozenset({"executed", "skipped_low_value", "skipped_disabled", "failed"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Histogram

    WEB_SEARCH_DECISIONS_TOTAL = Counter(
        "ratatoskr_web_search_decisions_total",
        "Total web-search enrichment decisions by outcome",
        ["decision"],
        registry=REGISTRY,
    )
    WEB_SEARCH_QUERY_RESULTS = Histogram(
        "ratatoskr_web_search_query_results",
        "Number of articles returned by each web-search query",
        buckets=[0, 1, 2, 3, 5, 10, 20, 50],
        registry=REGISTRY,
    )
else:
    WEB_SEARCH_DECISIONS_TOTAL = None
    WEB_SEARCH_QUERY_RESULTS = None


def record_web_search_decision(decision: str) -> None:
    """Record one web-search enrichment decision."""
    if not PROMETHEUS_AVAILABLE:
        return
    label = decision if decision in _KNOWN_DECISIONS else "failed"
    WEB_SEARCH_DECISIONS_TOTAL.labels(decision=label).inc()


def record_web_search_query_results(result_count: int) -> None:
    """Record how many articles a single web-search query returned."""
    if not PROMETHEUS_AVAILABLE:
        return
    WEB_SEARCH_QUERY_RESULTS.observe(max(0, result_count))
