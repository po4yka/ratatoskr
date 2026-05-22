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
"""

from __future__ import annotations

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

    OPENROUTER_CIRCUIT_BREAKER_STATE = Gauge(
        "openrouter_circuit_breaker_state",
        "Per-model circuit breaker state (0=closed, 1=half_open, 2=open).",
        ["model", "state"],
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
    SCRAPER_ATTEMPTS_TOTAL = None
    SCRAPER_ATTEMPT_LATENCY_SECONDS = None


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

    if prompt_tokens > 0:
        OPENROUTER_TOKENS.labels(model=model, type="prompt").inc(prompt_tokens)

    if completion_tokens > 0:
        OPENROUTER_TOKENS.labels(model=model, type="completion").inc(completion_tokens)

    if cost_usd > 0:
        OPENROUTER_COST_USD.inc(cost_usd)

    if latency_seconds is not None:
        OPENROUTER_LATENCY.labels(model=model).observe(latency_seconds)


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
        platform=platform,
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
    OPENROUTER_PER_MODEL_TIMEOUT.labels(model=model).inc()


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
    OPENROUTER_PER_MODEL_LATENCY.labels(model=model, outcome=outcome).observe(seconds)


def record_per_model_circuit_breaker_state(model: str, state: str) -> None:
    """Update the per-model circuit breaker state gauge.

    Uses a label-per-state pattern: each ``(model, state)`` combination
    is set to 1.0 when active and 0.0 when inactive, so dashboards can
    filter on ``state`` label values directly.

    Args:
        model: Model name whose breaker changed state.
        state: One of ``closed``, ``open``, ``half_open``.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    for s in ("closed", "open", "half_open"):
        OPENROUTER_CIRCUIT_BREAKER_STATE.labels(model=model, state=s).set(
            1.0 if s == state else 0.0
        )


def record_openrouter_stream_fallback(model: str, reason: str) -> None:
    """Record an OpenRouter SSE stream fallback to non-streaming.

    Args:
        model: Model name that triggered the fallback
        reason: One of stream_request_failed, stream_consumed_early, non_streaming_chunk_path
    """
    if not PROMETHEUS_AVAILABLE:
        return
    OPENROUTER_STREAM_FALLBACK.labels(model=model, reason=reason).inc()


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
    LLM_CALL_ATTEMPTS_TOTAL.labels(provider=provider, model=model, status=status).inc()


def record_llm_call_retry_exhaustion(*, model: str) -> None:
    """Record that the entire LLM fallback chain was exhausted for a request.

    Should be incremented once per request (not once per attempt) when
    no model in the cascade produced a successful response.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    LLM_CALL_RETRY_EXHAUSTION_TOTAL.labels(model=model).inc()


def record_llm_call_latency(*, model: str, latency_seconds: float) -> None:
    """Record per-attempt LLM call latency.

    Negative values are silently dropped (prometheus rejects them
    and a buggy caller should not crash the request hot-path).
    """
    if not PROMETHEUS_AVAILABLE:
        return
    if latency_seconds < 0:
        return
    LLM_CALL_LATENCY_SECONDS.labels(model=model).observe(latency_seconds)


def record_llm_call_persisted(call: dict[str, Any]) -> None:
    """Record metrics for a persisted LLM call without exposing payload content."""
    if not PROMETHEUS_AVAILABLE:
        return

    provider = str(call.get("provider") or "unknown")
    model = str(call.get("model") or "unknown")
    status = str(call.get("status") or "unknown")
    prompt_tokens = int(call.get("tokens_prompt") or 0)
    completion_tokens = int(call.get("tokens_completion") or 0)
    cost_usd = call.get("cost_usd")
    latency_ms = call.get("latency_ms")

    LLM_CALL_ATTEMPTS_TOTAL.labels(provider=provider, model=model, status=status).inc()
    if prompt_tokens > 0:
        LLM_TOKENS_TOTAL.labels(provider=provider, model=model, type="prompt").inc(prompt_tokens)
    if completion_tokens > 0:
        LLM_TOKENS_TOTAL.labels(provider=provider, model=model, type="completion").inc(
            completion_tokens
        )
    if cost_usd is not None and float(cost_usd) > 0:
        LLM_COST_USD_TOTAL.labels(provider=provider, model=model).inc(float(cost_usd))
    if latency_ms is not None:
        latency_seconds = max(0.0, float(latency_ms) / 1000.0)
        LLM_CALL_LATENCY_SECONDS.labels(model=model).observe(latency_seconds)

    error_text = str(call.get("error_text") or "").lower()
    error_context = call.get("error_context_json") or {}
    context_message = ""
    if isinstance(error_context, dict):
        context_message = str(error_context.get("message") or "").lower()
    stage = _parse_failure_stage(error_text=error_text, context_message=context_message)
    if stage is not None:
        LLM_PARSE_FAILURES_TOTAL.labels(provider=provider, model=model, stage=stage).inc()
    if "timeout" in error_text or "timeout" in context_message:
        LLM_TIMEOUTS_TOTAL.labels(provider=provider, model=model).inc()

    attempt_trigger = str(call.get("attempt_trigger") or "")
    if attempt_trigger == "repair_loop":
        LLM_REPAIR_ATTEMPTS_TOTAL.labels(provider=provider, model=model, status=status).inc()
    if attempt_trigger == "auto_backfill" or call.get("fallback_model_used"):
        LLM_FALLBACK_ATTEMPTS_TOTAL.labels(provider=provider, model=model, status=status).inc()


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
