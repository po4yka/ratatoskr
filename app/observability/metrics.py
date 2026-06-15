"""Prometheus metrics for Ratatoskr.

This module provides metrics collection for monitoring the application's:
- Request throughput and latency
- Firecrawl API usage
- OpenRouter API usage and costs
- Circuit breaker states
- Database query performance

Usage:
    from app.observability.metrics import record_request, record_firecrawl_request

    # Record a request
    record_request(request_type="url", status="success", source="telegram")

    # Record Firecrawl API call
    record_firecrawl_request(status="success", endpoint="scrape", latency_ms=1500)

Cardinality ceiling
-------------------
LLM/OpenRouter metrics use ``_bucket_model()`` to cap the ``model`` label to the
configured allowlist plus the ``"other"`` catch-all.  With a typical deployment
of 5-8 actively tracked models the ceiling is::

    (len(MODEL_LABEL_ALLOWLIST) + 1) x per-metric label combinations

For the default allowlist of 9 entries that means at most 10 model label values
per metric family, keeping Pi-hosted Prometheus well within its series budget
even as OpenRouter experiments add new model IDs to the fallback chain.

The ``platform`` label on ``AGGREGATION_EXTRACTION`` is bounded to
``_KNOWN_PLATFORMS`` (8 values + ``"other"``), preventing free-form leakage
from new social-platform source kinds.
"""

from __future__ import annotations

import threading
from typing import Any

from app.core.logging_utils import get_logger

# Try to import prometheus_client, but make it optional
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Model-label bucketing — caps cardinality for all LLM/OpenRouter metrics
# ---------------------------------------------------------------------------
# Default allowlist: the primary model + all default fallback models defined
# in OpenRouterConfig and ModelRoutingConfig.  Operators who override
# OPENROUTER_MODEL / OPENROUTER_FALLBACK_MODELS at runtime should call
# configure_model_allowlist() during DI setup so that their custom models are
# tracked individually instead of collapsing into "other".
_DEFAULT_MODEL_ALLOWLIST: frozenset[str] = frozenset(
    {
        # OpenRouterConfig defaults
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.6-flash",
        "qwen/qwen3.6-plus-04-02",
        "moonshotai/kimi-k2-0905",
        "minimax/minimax-m2",
        # ModelRoutingConfig defaults
        "deepseek/deepseek-v4-pro",
        "x-ai/grok-4.20-beta",
        # Flash model + flash fallback
        # (qwen/qwen3.6-flash already listed above)
        # Long-context alias
        # (minimax/minimax-m2 already listed above)
    }
)

_model_allowlist_lock = threading.Lock()
_model_allowlist: frozenset[str] = _DEFAULT_MODEL_ALLOWLIST


def configure_model_allowlist(models: frozenset[str] | set[str]) -> None:
    """Replace the model-label allowlist with the operator-configured set.

    Call this once during DI setup, passing the union of all model IDs that
    should be tracked as individual label values.  Any model not in the set
    will be reported as ``"other"``.

    Example (in DI setup)::

        from app.observability.metrics import configure_model_allowlist
        configure_model_allowlist(
            {cfg.openrouter.model, *cfg.openrouter.fallback_models}
        )
    """
    global _model_allowlist
    with _model_allowlist_lock:
        _model_allowlist = frozenset(models) | {"other"}


def _bucket_model(model: str) -> str:
    """Return *model* if it is in the allowlist, else ``"other"``.

    Thread-safe; reads the module-level ``_model_allowlist`` frozenset which
    is replaced atomically by :func:`configure_model_allowlist`.
    """
    return model if model in _model_allowlist else "other"


# Known platform values for AGGREGATION_EXTRACTION.
# Adding a new social-platform source kind requires updating this set AND the
# _platform_from_source_kind() mapping in multi_source_extraction_agent.py.
_KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {"twitter", "instagram", "telegram", "threads", "youtube", "web", "unknown"}
)


def _bucket_platform(platform: str) -> str:
    """Return *platform* if it is a known platform, else ``"other"``."""
    return platform if platform in _KNOWN_PLATFORMS else "other"


# ---------------------------------------------------------------------------

# Create a custom registry to avoid conflicts with default registry
if PROMETHEUS_AVAILABLE:
    REGISTRY = CollectorRegistry()

    # Request metrics
    REQUESTS_TOTAL = Counter(
        "ratatoskr_requests_total",
        "Total number of requests processed",
        ["type", "status", "source"],
        registry=REGISTRY,
    )

    REQUEST_LATENCY = Histogram(
        "ratatoskr_request_latency_seconds",
        "Request latency in seconds",
        ["type", "stage"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
        registry=REGISTRY,
    )

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

    # OpenRouter metrics
    OPENROUTER_TOKENS = Counter(
        "ratatoskr_openrouter_tokens_total",
        "Total tokens used in OpenRouter API calls",
        ["model", "type"],
        registry=REGISTRY,
    )

    OPENROUTER_COST_USD = Counter(
        "ratatoskr_openrouter_cost_usd_total",
        "Total cost in USD for OpenRouter API calls",
        registry=REGISTRY,
    )

    OPENROUTER_LATENCY = Histogram(
        "ratatoskr_openrouter_latency_seconds",
        "OpenRouter API latency in seconds",
        ["model"],
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
        registry=REGISTRY,
    )

    # ---- LLM retry-budget telemetry -------------------------------------
    # Per-attempt counter: tracks every LLM call we issue, including
    # fallback-model retries, so OpenRouter outages and prompt regressions
    # surface as visible spikes in retry rate.
    LLM_CALL_ATTEMPTS_TOTAL = Counter(
        "ratatoskr_llm_call_attempts_total",
        "Total LLM call attempts across the retry loop",
        ["provider", "model", "status"],
        registry=REGISTRY,
    )
    # Triggered once per request when the entire fallback chain has been
    # exhausted without success — paired with an alert recipe in docs.
    LLM_CALL_RETRY_EXHAUSTION_TOTAL = Counter(
        "ratatoskr_llm_call_retry_exhaustion_total",
        "Total LLM requests that exhausted the full fallback chain",
        ["model"],
        registry=REGISTRY,
    )
    LLM_CALL_LATENCY_SECONDS = Histogram(
        "ratatoskr_llm_call_latency_seconds",
        "End-to-end latency of a single LLM call attempt in seconds",
        ["model"],
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
        registry=REGISTRY,
    )
    LLM_TOKENS_TOTAL = Counter(
        "ratatoskr_llm_tokens_total",
        "Total LLM tokens persisted by provider, model, and token type",
        ["provider", "model", "type"],
        registry=REGISTRY,
    )
    LLM_COST_USD_TOTAL = Counter(
        "ratatoskr_llm_cost_usd_total",
        "Total estimated LLM cost in USD by provider and model",
        ["provider", "model"],
        registry=REGISTRY,
    )
    LLM_PARSE_FAILURES_TOTAL = Counter(
        "ratatoskr_llm_parse_failures_total",
        "Total LLM parse failures by provider, model, and failure stage",
        ["provider", "model", "stage"],
        registry=REGISTRY,
    )
    LLM_REPAIR_ATTEMPTS_TOTAL = Counter(
        "ratatoskr_llm_repair_attempts_total",
        "Total LLM JSON repair attempts by provider, model, and status",
        ["provider", "model", "status"],
        registry=REGISTRY,
    )
    LLM_FALLBACK_ATTEMPTS_TOTAL = Counter(
        "ratatoskr_llm_fallback_attempts_total",
        "Total LLM fallback attempts by provider, model, and status",
        ["provider", "model", "status"],
        registry=REGISTRY,
    )
    LLM_TIMEOUTS_TOTAL = Counter(
        "ratatoskr_llm_timeouts_total",
        "Total LLM timeout outcomes by provider and model",
        ["provider", "model"],
        registry=REGISTRY,
    )

    # ---- Per-request total LLM cost -------------------------------------
    # End-to-end wall time spent across every LLM call for a single user
    # request (sum of llm_calls.latency_ms for the request). Pathological
    # cases like the 2026-05-22 Habr-vision-routing incident showed 700+s
    # of LLM thrash on one request; this metric makes such regressions
    # visible on dashboards instead of surfacing only via complaints.
    LLM_REQUEST_TOTAL_LATENCY_SECONDS = Histogram(
        "ratatoskr_llm_request_total_latency_seconds",
        "Total per-request LLM wall time across all attempts in seconds",
        ["request_type"],
        buckets=[1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0],
        registry=REGISTRY,
    )
    LLM_REQUEST_SLOW_TOTAL = Counter(
        "ratatoskr_llm_request_slow_total",
        "Requests whose total LLM wall time exceeded the slow-request threshold",
        ["request_type"],
        registry=REGISTRY,
    )

    # ---- Scraper chain telemetry ---------------------------------------
    # Per-provider attempt counter (status: success | error | timeout |
    # skipped) and per-provider latency histogram. Lets operators see
    # provider drift and pick which provider to drop from the chain.
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
    # End-to-end wall time for a full chain invocation (sum of all tier
    # attempts, including races). Tier-1 of the speedup plan racing free
    # providers and reading P50/P95 movement here is how we know the win
    # actually landed.
    SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS = Histogram(
        "ratatoskr_scraper_chain_total_latency_seconds",
        "Total wall time for one scraper chain invocation in seconds",
        ["mode", "outcome"],
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 60.0, 90.0, 120.0],
        registry=REGISTRY,
    )
    # Per-provider failure breakdown by reason. Complements SCRAPER_ATTEMPTS_TOTAL
    # (which records every attempt with status=error|success|skipped) by exposing
    # the WHY behind each failure so operators can distinguish empty responses,
    # quality rejections, and hard errors in a single metric family.
    SCRAPER_CHAIN_FAILURES_TOTAL = Counter(
        "ratatoskr_scraper_chain_failures_total",
        "Scraper chain provider failures by provider and failure reason",
        ["provider", "reason"],
        registry=REGISTRY,
    )

    # ---- Scraper chain per-provider summary counters and duration -------
    # Tracks attempts, successes, and failures per provider at the chain
    # level (one observation per chain invocation, not per tier attempt).
    # Complements SCRAPER_ATTEMPTS_TOTAL (per-tier) and
    # SCRAPER_CHAIN_FAILURES_TOTAL (failure reason breakdown).
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

    # ---- Social integration telemetry ----------------------------------
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

    # Circuit breaker metrics
    CIRCUIT_BREAKER_STATE = Gauge(
        "ratatoskr_circuit_breaker_state",
        "Circuit breaker state (0=closed, 1=half_open, 2=open)",
        ["service"],
        registry=REGISTRY,
    )

    # Database metrics
    DB_QUERY_LATENCY = Histogram(
        "ratatoskr_db_query_latency_seconds",
        "Database query latency in seconds",
        ["operation"],
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
        registry=REGISTRY,
    )

    DB_CONNECTIONS = Gauge(
        "ratatoskr_db_connections_active",
        "Number of active database connections",
        registry=REGISTRY,
    )

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

    DRAFT_STREAM_EVENTS = Counter(
        "ratatoskr_draft_stream_events_total",
        "Draft/stream lifecycle events",
        ["event"],
        registry=REGISTRY,
    )

    STREAM_LATENCY_MS = Histogram(
        "ratatoskr_stream_latency_ms",
        "Streaming timing metrics in milliseconds",
        ["metric"],
        buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000],
        registry=REGISTRY,
    )

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

    # ---- URL worker queue telemetry ------------------------------------
    # Incremented by the bot each time it enqueues (or falls back to inline).
    URL_ENQUEUE_TOTAL = Counter(
        "ratatoskr_url_enqueue_total",
        "Bot URL enqueue outcomes",
        ["status"],
        registry=REGISTRY,
    )
    # Current depth of the URL processing queue (pending + failed-retryable).
    URL_PROCESSING_QUEUE_DEPTH = Gauge(
        "ratatoskr_url_processing_queue_depth",
        "Number of URL processing jobs waiting in the queue",
        registry=REGISTRY,
    )

    SCHEDULER_JOB_CHRONIC_FAILURES = Counter(
        "ratatoskr_scheduler_job_chronic_failures_total",
        "Scheduler jobs that have failed 3+ consecutive ticks",
        ["job_id"],
        registry=REGISTRY,
    )

    ADMIN_DIAGNOSTICS_REQUESTS = Counter(
        "ratatoskr_admin_diagnostics_requests_total",
        "Owner diagnostics API requests by outcome",
        ["status"],
        registry=REGISTRY,
    )

    VECTOR_INDEXING_LAG = Gauge(
        "ratatoskr_vector_indexing_lag",
        "Vector indexing reconciliation lag and drift counts",
        ["metric"],
        registry=REGISTRY,
    )

    VECTOR_WRITES_TOTAL = Counter(
        "ratatoskr_vector_writes_total",
        "Vector store write attempts by operation and status",
        ["operation", "status"],
        registry=REGISTRY,
    )

    OPENROUTER_STREAM_FALLBACK = Counter(
        "ratatoskr_openrouter_stream_fallback_total",
        "OpenRouter SSE stream fallbacks to non-streaming.",
        ["model", "reason"],
        registry=REGISTRY,
    )

    OPENROUTER_PER_MODEL_TIMEOUT = Counter(
        "openrouter_per_model_timeout_total",
        "Per-model timeouts in the OpenRouter fallback chain.",
        ["model"],
        registry=REGISTRY,
    )

    OPENROUTER_PER_MODEL_LATENCY = Histogram(
        "openrouter_per_model_latency_seconds",
        "Per-model latency in the OpenRouter fallback chain.",
        ["model", "outcome"],
        buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600],
        registry=REGISTRY,
    )

    # Single integer gauge per (bucketed) model: 0=closed, 1=half_open, 2=open.
    # This replaces the old 3-series-per-model label-per-state pattern, cutting
    # series count from 3xN to 1xN.
    OPENROUTER_CIRCUIT_BREAKER_STATE = Gauge(
        "openrouter_circuit_breaker_state",
        "Per-model circuit breaker state: 0=closed, 1=half_open, 2=open.",
        ["model"],
        registry=REGISTRY,
    )

    # ---- URL processor in-flight gauge (Phase 5c) -----------------------
    # Incremented when URLProcessor begins processing a request; decremented
    # in the finally-block.  Single-process gauge (one Docker container).
    URL_PROCESSOR_IN_FLIGHT = Gauge(
        "ratatoskr_url_processor_in_flight",
        "Number of URL processing requests currently active",
        registry=REGISTRY,
    )

    # ---- APScheduler / queue depth gauge (Phase 5c) ---------------------
    # Snapshot depth of any scheduler or background queue at reporting time.
    # Label "queue" distinguishes multiple queues (e.g. "url_processor",
    # "taskiq", "rss").
    SCHEDULER_QUEUE_DEPTH = Gauge(
        "ratatoskr_scheduler_queue_depth",
        "Current depth of a named background job queue",
        ["queue"],
        registry=REGISTRY,
    )

else:
    # Create dummy metrics when prometheus_client is not available
    REGISTRY = None
    REQUESTS_TOTAL = None
    REQUEST_LATENCY = None
    FIRECRAWL_REQUESTS = None
    FIRECRAWL_LATENCY = None
    OPENROUTER_TOKENS = None
    OPENROUTER_COST_USD = None
    OPENROUTER_LATENCY = None
    CIRCUIT_BREAKER_STATE = None
    DB_QUERY_LATENCY = None
    DB_CONNECTIONS = None
    TWITTER_ARTICLE_RESOLUTION = None
    TWITTER_ARTICLE_RESOLUTION_LATENCY = None
    TWITTER_ARTICLE_EXTRACTION = None
    EXTRACTION_FAILURES = None
    EXTRACTION_ATTEMPTS = None
    EXTRACTION_STAGE_LATENCY = None
    DRAFT_STREAM_EVENTS = None
    STREAM_LATENCY_MS = None
    AGGREGATION_EXTRACTION = None
    AGGREGATION_BUNDLES = None
    AGGREGATION_BUNDLE_LATENCY = None
    AGGREGATION_SYNTHESIS_COVERAGE = None
    AGGREGATION_USED_SOURCES = None
    AGGREGATION_COST_USD = None
    SCHEDULER_JOB_CHRONIC_FAILURES = None
    ADMIN_DIAGNOSTICS_REQUESTS = None
    VECTOR_INDEXING_LAG = None
    VECTOR_WRITES_TOTAL = None
    URL_ENQUEUE_TOTAL = None
    URL_PROCESSING_QUEUE_DEPTH = None
    OPENROUTER_STREAM_FALLBACK = None
    OPENROUTER_PER_MODEL_TIMEOUT = None
    OPENROUTER_PER_MODEL_LATENCY = None
    OPENROUTER_CIRCUIT_BREAKER_STATE = None
    LLM_CALL_ATTEMPTS_TOTAL = None
    LLM_CALL_RETRY_EXHAUSTION_TOTAL = None
    LLM_CALL_LATENCY_SECONDS = None
    LLM_TOKENS_TOTAL = None
    LLM_COST_USD_TOTAL = None
    LLM_PARSE_FAILURES_TOTAL = None
    LLM_REPAIR_ATTEMPTS_TOTAL = None
    LLM_FALLBACK_ATTEMPTS_TOTAL = None
    LLM_TIMEOUTS_TOTAL = None
    LLM_REQUEST_TOTAL_LATENCY_SECONDS = None
    LLM_REQUEST_SLOW_TOTAL = None
    SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS = None
    SCRAPER_ATTEMPTS_TOTAL = None
    SCRAPER_ATTEMPT_LATENCY_SECONDS = None
    SCRAPER_CHAIN_FAILURES_TOTAL = None
    SOCIAL_FETCH_TOTAL = None
    SOCIAL_TOKEN_REFRESH_TOTAL = None
    SOCIAL_RATE_LIMIT_TOTAL = None
    SOCIAL_CONNECTION_STATUS_TOTAL = None
    URL_PROCESSOR_IN_FLIGHT = None
    SCHEDULER_QUEUE_DEPTH = None
    SCRAPER_CHAIN_ATTEMPTS_TOTAL = None
    SCRAPER_CHAIN_SUCCESSES_TOTAL = None
    SCRAPER_CHAIN_DURATION_SECONDS = None


def get_metrics() -> bytes:
    """Generate Prometheus metrics in text format.

    Returns:
        Prometheus metrics as bytes in text format, or empty bytes if unavailable.
    """
    if not PROMETHEUS_AVAILABLE or REGISTRY is None:
        return b"# Prometheus metrics not available (prometheus_client not installed)\n"
    return generate_latest(REGISTRY)


def get_metrics_content_type() -> str:
    """Get the content type for Prometheus metrics response."""
    if PROMETHEUS_AVAILABLE:
        return CONTENT_TYPE_LATEST
    return "text/plain; charset=utf-8"


def record_request(
    request_type: str,
    status: str,
    source: str,
    latency_seconds: float | None = None,
    stage: str = "total",
) -> None:
    """Record a request metric.

    Args:
        request_type: Type of request (url, forward, command)
        status: Request status (success, error, timeout)
        source: Request source (telegram, api, cli)
        latency_seconds: Optional latency in seconds
        stage: Processing stage (extraction, summarization, total)
    """
    if not PROMETHEUS_AVAILABLE:
        return

    REQUESTS_TOTAL.labels(type=request_type, status=status, source=source).inc()

    if latency_seconds is not None:
        REQUEST_LATENCY.labels(type=request_type, stage=stage).observe(latency_seconds)


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


def record_openrouter_call(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_seconds: float | None = None,
) -> None:
    """Record an OpenRouter API call metric.

    Args:
        model: Model name used for the call
        prompt_tokens: Number of prompt tokens used
        completion_tokens: Number of completion tokens used
        cost_usd: Cost of the call in USD
        latency_seconds: Optional latency in seconds
    """
    if not PROMETHEUS_AVAILABLE:
        return

    bucketed = _bucket_model(model)
    if prompt_tokens > 0:
        OPENROUTER_TOKENS.labels(model=bucketed, type="prompt").inc(prompt_tokens)

    if completion_tokens > 0:
        OPENROUTER_TOKENS.labels(model=bucketed, type="completion").inc(completion_tokens)

    if cost_usd > 0:
        OPENROUTER_COST_USD.inc(cost_usd)

    if latency_seconds is not None:
        OPENROUTER_LATENCY.labels(model=bucketed).observe(latency_seconds)


def record_circuit_breaker_state(service: str, state: str) -> None:
    """Record circuit breaker state.

    Args:
        service: Service name (firecrawl, openrouter)
        state: Circuit breaker state (closed, half_open, open)
    """
    if not PROMETHEUS_AVAILABLE:
        return

    state_value = {"closed": 0, "half_open": 1, "open": 2}.get(state, -1)
    CIRCUIT_BREAKER_STATE.labels(service=service).set(state_value)


def record_db_query(operation: str, latency_seconds: float) -> None:
    """Record a database query metric.

    Args:
        operation: Query operation type (select, insert, update, delete)
        latency_seconds: Query latency in seconds
    """
    if not PROMETHEUS_AVAILABLE:
        return

    DB_QUERY_LATENCY.labels(operation=operation).observe(latency_seconds)


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


def set_db_connections(count: int) -> None:
    """Set the number of active database connections.

    Args:
        count: Number of active connections
    """
    if not PROMETHEUS_AVAILABLE:
        return

    DB_CONNECTIONS.set(count)


def record_draft_stream_event(event: str, *, amount: int = 1) -> None:
    """Record a draft/stream event counter."""
    if not PROMETHEUS_AVAILABLE:
        return
    if amount <= 0:
        return
    DRAFT_STREAM_EVENTS.labels(event=event).inc(amount)


def record_scheduler_chronic_failure(job_id: str) -> None:
    """Increment the chronic-failure counter for a scheduler job."""
    if not PROMETHEUS_AVAILABLE:
        return
    SCHEDULER_JOB_CHRONIC_FAILURES.labels(job_id=job_id).inc()


def record_admin_diagnostics_request(status: str) -> None:
    """Record an owner diagnostics API request outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    ADMIN_DIAGNOSTICS_REQUESTS.labels(status=status).inc()


def record_stream_latency_ms(metric: str, value_ms: float) -> None:
    """Record stream latency-like metric in milliseconds."""
    if not PROMETHEUS_AVAILABLE:
        return
    if value_ms < 0:
        return
    STREAM_LATENCY_MS.labels(metric=metric).observe(value_ms)


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


def record_per_model_timeout(model: str) -> None:
    """Increment the per-model timeout counter for *model*.

    Args:
        model: The model name that timed out (e.g. ``deepseek/deepseek-v3.2``).
    """
    if not PROMETHEUS_AVAILABLE:
        return
    OPENROUTER_PER_MODEL_TIMEOUT.labels(model=_bucket_model(model)).inc()


def record_per_model_latency(model: str, outcome: str, seconds: float) -> None:
    """Observe per-model latency for the OpenRouter fallback chain.

    Args:
        model: Model name used in the attempt.
        outcome: One of ``success``, ``timeout``, ``error``,
            ``skipped_unsupported_structured``, ``circuit_open``.
        seconds: Wall-clock duration of the per-model attempt in seconds.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    OPENROUTER_PER_MODEL_LATENCY.labels(model=_bucket_model(model), outcome=outcome).observe(
        seconds
    )


def record_per_model_circuit_breaker_state(model: str, state: str) -> None:
    """Update the per-model circuit breaker state gauge.

    Writes a single integer per bucketed model: 0=closed, 1=half_open, 2=open.
    This replaces the former 3-series-per-model label-per-state pattern, which
    produced 3xN Prometheus series.  Dashboards should alert/filter on
    ``openrouter_circuit_breaker_state >= 1`` (any non-closed state).

    Args:
        model: Model name whose breaker changed state.
        state: One of ``closed``, ``open``, ``half_open``.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    state_int = {"closed": 0, "half_open": 1, "open": 2}.get(state, 0)
    OPENROUTER_CIRCUIT_BREAKER_STATE.labels(model=_bucket_model(model)).set(state_int)


def record_openrouter_stream_fallback(model: str, reason: str) -> None:
    """Record an OpenRouter SSE stream fallback to non-streaming.

    Args:
        model: Model name that triggered the fallback
        reason: One of stream_request_failed, stream_consumed_early, non_streaming_chunk_path
    """
    if not PROMETHEUS_AVAILABLE:
        return
    OPENROUTER_STREAM_FALLBACK.labels(model=_bucket_model(model), reason=reason).inc()


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


def record_llm_call_attempt(*, provider: str, model: str, status: str) -> None:
    """Record a single LLM call attempt.

    Args:
        provider: Upstream provider name (``openrouter``, ``openai``,
            ``anthropic``, etc.) — distinct from the OpenRouter
            ``metadata.provider_name`` which identifies the *upstream*
            of OpenRouter.
        model: Model identifier used for the call.
        status: ``success`` | ``error`` | ``soft_failure`` |
            ``timeout``. Free-form to allow callers to add finer
            granularity without breaking the schema.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    LLM_CALL_ATTEMPTS_TOTAL.labels(
        provider=provider, model=_bucket_model(model), status=status
    ).inc()


def record_llm_call_retry_exhaustion(*, model: str) -> None:
    """Record that the entire LLM fallback chain was exhausted for a request.

    Should be incremented once per request (not once per attempt) when
    no model in the cascade produced a successful response.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    LLM_CALL_RETRY_EXHAUSTION_TOTAL.labels(model=_bucket_model(model)).inc()


def record_llm_call_latency(*, model: str, latency_seconds: float) -> None:
    """Record per-attempt LLM call latency.

    Negative values are silently dropped (prometheus rejects them
    and a buggy caller should not crash the request hot-path).
    """
    if not PROMETHEUS_AVAILABLE:
        return
    if latency_seconds < 0:
        return
    LLM_CALL_LATENCY_SECONDS.labels(model=_bucket_model(model)).observe(latency_seconds)


def record_llm_call_persisted(call: dict[str, Any]) -> None:
    """Record metrics for a persisted LLM call without exposing payload content."""
    if not PROMETHEUS_AVAILABLE:
        return

    provider = str(call.get("provider") or "unknown")
    model = str(call.get("model") or "unknown")
    bucketed = _bucket_model(model)
    status = str(call.get("status") or "unknown")
    prompt_tokens = int(call.get("tokens_prompt") or 0)
    completion_tokens = int(call.get("tokens_completion") or 0)
    cost_usd = call.get("cost_usd")
    latency_ms = call.get("latency_ms")

    LLM_CALL_ATTEMPTS_TOTAL.labels(provider=provider, model=bucketed, status=status).inc()
    if prompt_tokens > 0:
        LLM_TOKENS_TOTAL.labels(provider=provider, model=bucketed, type="prompt").inc(prompt_tokens)
    if completion_tokens > 0:
        LLM_TOKENS_TOTAL.labels(provider=provider, model=bucketed, type="completion").inc(
            completion_tokens
        )
    if cost_usd is not None and float(cost_usd) > 0:
        LLM_COST_USD_TOTAL.labels(provider=provider, model=bucketed).inc(float(cost_usd))
    if latency_ms is not None:
        latency_seconds = max(0.0, float(latency_ms) / 1000.0)
        LLM_CALL_LATENCY_SECONDS.labels(model=bucketed).observe(latency_seconds)

    error_text = str(call.get("error_text") or "").lower()
    error_context = call.get("error_context_json") or {}
    context_message = ""
    if isinstance(error_context, dict):
        context_message = str(error_context.get("message") or "").lower()
    stage = _parse_failure_stage(error_text=error_text, context_message=context_message)
    if stage is not None:
        LLM_PARSE_FAILURES_TOTAL.labels(provider=provider, model=bucketed, stage=stage).inc()
    if "timeout" in error_text or "timeout" in context_message:
        LLM_TIMEOUTS_TOTAL.labels(provider=provider, model=bucketed).inc()

    attempt_trigger = str(call.get("attempt_trigger") or "")
    if attempt_trigger == "repair_loop":
        LLM_REPAIR_ATTEMPTS_TOTAL.labels(provider=provider, model=bucketed, status=status).inc()
    if attempt_trigger == "auto_backfill" or call.get("fallback_model_used"):
        LLM_FALLBACK_ATTEMPTS_TOTAL.labels(provider=provider, model=bucketed, status=status).inc()


def _parse_failure_stage(*, error_text: str, context_message: str) -> str | None:
    combined = f"{error_text} {context_message}"
    if "json_parse_timeout" in combined:
        return "json_parse_timeout"
    if "summary_parse_failed" in combined or "structured_output_parse_error" in combined:
        return "summary_parse_failed"
    if "json_repair_failed" in combined or "repair_failed" in combined:
        return "json_repair_failed"
    if "parse json" in combined or "failed to parse" in combined or "validation" in combined:
        return "provider_response_parse"
    return None


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


def _metric_label(value: Any) -> str:
    label = str(value or "unknown").strip().lower()
    return label or "unknown"


def record_vector_index_lag(report: dict[str, Any]) -> None:
    """Record vector reconciliation gauges from a diagnostics report."""
    if not PROMETHEUS_AVAILABLE or VECTOR_INDEXING_LAG is None:
        return
    for metric in (
        "lag_seconds",
        "missing_summary_vectors",
        "missing_repository_vectors",
        "stale_embedding_model_count",
        "missing_embeddings",
        "stale_embeddings",
    ):
        value = report.get(metric)
        if value is None:
            continue
        VECTOR_INDEXING_LAG.labels(metric=metric).set(float(value))


def record_vector_write(*, operation: str, status: str) -> None:
    """Record a vector-store write outcome."""
    if not PROMETHEUS_AVAILABLE or VECTOR_WRITES_TOTAL is None:
        return
    VECTOR_WRITES_TOTAL.labels(operation=operation, status=status).inc()


def record_url_enqueue(*, status: str) -> None:
    """Record a bot URL enqueue outcome.

    Args:
        status: ``success`` | ``skipped_inline`` | ``failed``
    """
    if not PROMETHEUS_AVAILABLE or URL_ENQUEUE_TOTAL is None:
        return
    URL_ENQUEUE_TOTAL.labels(status=_metric_label(status)).inc()


def set_url_processing_queue_depth(depth: int) -> None:
    """Update the URL processing queue depth gauge."""
    if not PROMETHEUS_AVAILABLE or URL_PROCESSING_QUEUE_DEPTH is None:
        return
    if depth < 0:
        return
    URL_PROCESSING_QUEUE_DEPTH.set(depth)


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
        outcome: ``success`` | ``empty`` | ``ssrf_blocked``.
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


def set_url_processor_in_flight(delta: int) -> None:
    """Increment or decrement the URL processor in-flight gauge.

    Call with delta=+1 when URLProcessor begins a request and delta=-1 in
    the finally-block when it completes.  Uses inc()/dec() to be safe under
    concurrent calls from the same process.

    Args:
        delta: +1 to increment (request started), -1 to decrement (request done).
    """
    if not PROMETHEUS_AVAILABLE or URL_PROCESSOR_IN_FLIGHT is None:
        return
    if delta > 0:
        URL_PROCESSOR_IN_FLIGHT.inc(delta)
    elif delta < 0:
        URL_PROCESSOR_IN_FLIGHT.dec(-delta)


def set_scheduler_queue_depth(queue: str, depth: int) -> None:
    """Set the current depth of a named background job queue.

    Args:
        queue: Queue name label (e.g. "url_processor", "taskiq", "rss").
        depth: Current number of waiting jobs.  Negative values are silently
            ignored.
    """
    if not PROMETHEUS_AVAILABLE or SCHEDULER_QUEUE_DEPTH is None:
        return
    if depth < 0:
        return
    SCHEDULER_QUEUE_DEPTH.labels(queue=_metric_label(queue)).set(depth)


def record_llm_request_total_latency(
    *,
    request_type: str,
    total_latency_seconds: float,
    slow_threshold_seconds: float = 300.0,
) -> None:
    """Record the end-to-end LLM wall time for a single user request.

    ``request_type`` is a coarse bucket label (e.g. ``url``, ``forward``,
    ``rss``) so dashboards can filter without enumerating every pipeline
    variant. Increments ``LLM_REQUEST_SLOW_TOTAL`` when the latency exceeds
    ``slow_threshold_seconds`` (default 300 s; override via
    ``LLM_REQUEST_SLOW_THRESHOLD_SEC`` env var on ``RuntimeConfig``).
    """
    if not PROMETHEUS_AVAILABLE:
        return
    if total_latency_seconds < 0:
        return
    label = _metric_label(request_type)
    LLM_REQUEST_TOTAL_LATENCY_SECONDS.labels(request_type=label).observe(total_latency_seconds)
    if total_latency_seconds >= slow_threshold_seconds:
        LLM_REQUEST_SLOW_TOTAL.labels(request_type=label).inc()


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
