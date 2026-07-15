"""Prometheus metrics for Ratatoskr — facade module.

This module re-exports every public name from the domain-scoped submodules so
that all existing ``from app.observability.metrics import X`` imports continue
to work unchanged.

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

Sub-module layout
-----------------
- ``_metrics_base``           — shared registry, prometheus import guard, bucketing helpers
- ``metrics_request``         — request throughput, URL queue
- ``metrics_scraper``         — Firecrawl + scraper chain
- ``metrics_llm``             — OpenRouter + LLM retry budget, per-model circuit breaker
- ``metrics_auth``            — auth/session security decisions
- ``metrics_cache``           — shared Redis JSON cache outcomes and latency
- ``metrics_mcp``             — MCP exposure posture
- ``metrics_circuit_breaker`` — service-level circuit breaker gauge
- ``metrics_twitter``         — Twitter/X extraction + generic extraction pipeline
- ``metrics_aggregation``     — multi-source aggregation
- ``metrics_streaming``       — draft stream events + stream latency
- ``metrics_social``          — social provider integrations
- ``metrics_tts``             — text-to-speech provider requests
- ``metrics_db``              — database query latency + admin diagnostics
- ``metrics_vector``          — vector store writes + indexing lag
- ``metrics_digest``          — Telegram channel digest delivery + userbot health
- ``metrics_scheduler``       — APScheduler / queue depth
- ``metrics_repositories``    — GitHub repo sync (pre-existing sibling module)
- ``metrics_web_search``      — optional web-search enrichment decisions
"""

from __future__ import annotations

import threading
from typing import Any

from app.core.logging_utils import get_logger

# ---------------------------------------------------------------------------
# prometheus_client re-exports (optional dep — same fail-open guard as original)
# ---------------------------------------------------------------------------
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Base: PROMETHEUS_AVAILABLE, REGISTRY, bucketing helpers
# ---------------------------------------------------------------------------
from app.observability._metrics_base import (
    _DEFAULT_MODEL_ALLOWLIST,
    _KNOWN_PLATFORMS,
    PROMETHEUS_AVAILABLE,
    REGISTRY,
    _bucket_model,
    _bucket_platform,
    _metric_label,
    _model_allowlist,
    _model_allowlist_lock,
    _parse_failure_stage,
    configure_model_allowlist,
)

# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
from app.observability.metrics_aggregation import (
    AGGREGATION_BUNDLE_LATENCY,
    AGGREGATION_BUNDLES,
    AGGREGATION_COST_USD,
    AGGREGATION_EXTRACTION,
    AGGREGATION_SYNTHESIS_COVERAGE,
    AGGREGATION_USED_SOURCES,
    record_aggregation_bundle,
    record_aggregation_extraction,
    record_aggregation_synthesis,
)

# ---------------------------------------------------------------------------
# Auth / sessions
# ---------------------------------------------------------------------------
from app.observability.metrics_auth import (
    RATE_LIMIT_HITS_TOTAL,
    TOKEN_FAMILY_DECISIONS_TOTAL,
    record_rate_limit_hit,
    record_token_family_decision,
)

# ---------------------------------------------------------------------------
# Shared Redis cache
# ---------------------------------------------------------------------------
from app.observability.metrics_cache import (
    REDIS_CACHE_OPERATION_LATENCY_SECONDS,
    REDIS_CACHE_OPERATIONS_TOTAL,
    cache_namespace_family,
    record_redis_cache_operation,
)

# ---------------------------------------------------------------------------
# Circuit breaker (service-level)
# ---------------------------------------------------------------------------
from app.observability.metrics_circuit_breaker import (
    CIRCUIT_BREAKER_STATE,
    record_circuit_breaker_state,
)

# ---------------------------------------------------------------------------
# Database + admin diagnostics
# ---------------------------------------------------------------------------
from app.observability.metrics_db import (
    ADMIN_DIAGNOSTICS_REQUESTS,
    DB_CONNECTIONS,
    DB_QUERY_LATENCY,
    record_admin_diagnostics_request,
    record_db_query,
    set_db_connections,
)

# ---------------------------------------------------------------------------
# Telegram channel digest
# ---------------------------------------------------------------------------
from app.observability.metrics_digest import (
    DIGEST_ACTIVE_SUBSCRIPTION_USERS,
    DIGEST_CHANNEL_FETCH_ERRORS_TOTAL,
    DIGEST_DELIVERIES_TOTAL,
    DIGEST_PIPELINE_DURATION_SECONDS,
    DIGEST_POSTS_ANALYZED_TOTAL,
    DIGEST_USERBOT_RECONNECTS_TOTAL,
    record_digest_channel_fetch_error,
    record_digest_delivery,
    record_digest_pipeline_duration,
    record_digest_posts_analyzed,
    record_digest_userbot_reconnect,
    set_digest_active_subscription_users,
)

# ---------------------------------------------------------------------------
# LLM / OpenRouter
# ---------------------------------------------------------------------------
from app.observability.metrics_llm import (
    LLM_CALL_ATTEMPTS_TOTAL,
    LLM_CALL_LATENCY_SECONDS,
    LLM_CALL_RETRY_EXHAUSTION_TOTAL,
    LLM_COST_USD_TOTAL,
    LLM_FALLBACK_ATTEMPTS_TOTAL,
    LLM_PARSE_FAILURES_TOTAL,
    LLM_REPAIR_ATTEMPTS_TOTAL,
    LLM_REQUEST_SLOW_TOTAL,
    LLM_REQUEST_TOTAL_LATENCY_SECONDS,
    LLM_TIMEOUTS_TOTAL,
    LLM_TOKENS_TOTAL,
    OPENROUTER_CIRCUIT_BREAKER_STATE,
    OPENROUTER_COST_USD,
    OPENROUTER_LATENCY,
    OPENROUTER_PER_MODEL_LATENCY,
    OPENROUTER_PER_MODEL_TIMEOUT,
    OPENROUTER_STREAM_FALLBACK,
    OPENROUTER_TOKENS,
    record_llm_call_attempt,
    record_llm_call_latency,
    record_llm_call_persisted,
    record_llm_call_retry_exhaustion,
    record_llm_request_total_latency,
    record_openrouter_call,
    record_openrouter_stream_fallback,
    record_per_model_circuit_breaker_state,
    record_per_model_latency,
    record_per_model_timeout,
)

# ---------------------------------------------------------------------------
# MCP exposure posture
# ---------------------------------------------------------------------------
from app.observability.metrics_mcp import (
    MCP_UNSCOPED_ENABLED,
    set_mcp_unscoped_enabled,
)

# ---------------------------------------------------------------------------
# Request / URL queue
# ---------------------------------------------------------------------------
from app.observability.metrics_request import (
    REQUEST_LATENCY,
    REQUESTS_TOTAL,
    URL_ENQUEUE_TOTAL,
    URL_PROCESSING_QUEUE_DEPTH,
    URL_PROCESSOR_IN_FLIGHT,
    record_request,
    record_url_enqueue,
    set_url_processing_queue_depth,
    set_url_processor_in_flight,
)

# ---------------------------------------------------------------------------
# Scheduler / queue depth
# ---------------------------------------------------------------------------
from app.observability.metrics_scheduler import (
    SCHEDULER_JOB_CHRONIC_FAILURES,
    SCHEDULER_QUEUE_DEPTH,
    TASKIQ_RETRIES_TOTAL,
    record_scheduler_chronic_failure,
    record_taskiq_retry_outcome,
    set_scheduler_queue_depth,
)

# ---------------------------------------------------------------------------
# Scraper chain (Firecrawl + multi-provider)
# ---------------------------------------------------------------------------
from app.observability.metrics_scraper import (
    FIRECRAWL_LATENCY,
    FIRECRAWL_REQUESTS,
    SCRAPER_ATTEMPT_LATENCY_SECONDS,
    SCRAPER_ATTEMPTS_TOTAL,
    SCRAPER_CHAIN_ATTEMPTS_TOTAL,
    SCRAPER_CHAIN_DURATION_SECONDS,
    SCRAPER_CHAIN_FAILURES_TOTAL,
    SCRAPER_CHAIN_SUCCESSES_TOTAL,
    SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS,
    record_firecrawl_request,
    record_scraper_attempt,
    record_scraper_attempt_latency,
    record_scraper_chain_attempt,
    record_scraper_chain_duration,
    record_scraper_chain_failure,
    record_scraper_chain_success,
    record_scraper_chain_total_latency,
)

# ---------------------------------------------------------------------------
# Social
# ---------------------------------------------------------------------------
from app.observability.metrics_social import (
    SOCIAL_CONNECTION_STATUS_TOTAL,
    SOCIAL_FETCH_TOTAL,
    SOCIAL_RATE_LIMIT_TOTAL,
    SOCIAL_TOKEN_REFRESH_TOTAL,
    record_social_connection_status,
    record_social_fetch,
    record_social_rate_limit,
    record_social_token_refresh,
)

# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
from app.observability.metrics_streaming import (
    DRAFT_STREAM_EVENTS,
    STREAM_LATENCY_MS,
    record_draft_stream_event,
    record_stream_latency_ms,
)

# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------
from app.observability.metrics_stt import (
    STT_AUDIO_SECONDS_TOTAL,
    STT_REQUESTS_TOTAL,
    record_stt_audio_seconds,
    record_stt_request,
)

# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------
from app.observability.metrics_tts import (
    TTS_AUDIO_BYTES_TOTAL,
    TTS_LATENCY_SECONDS,
    TTS_REQUESTS_TOTAL,
    record_tts_audio_bytes,
    record_tts_latency,
    record_tts_request,
)

# ---------------------------------------------------------------------------
# Twitter/X + generic extraction pipeline
# ---------------------------------------------------------------------------
from app.observability.metrics_twitter import (
    EXTRACTION_ATTEMPTS,
    EXTRACTION_FAILURES,
    EXTRACTION_STAGE_LATENCY,
    TWITTER_ARTICLE_EXTRACTION,
    TWITTER_ARTICLE_RESOLUTION,
    TWITTER_ARTICLE_RESOLUTION_LATENCY,
    record_extraction_attempt,
    record_extraction_failure,
    record_twitter_article_extraction,
    record_twitter_article_resolution,
)

# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------
from app.observability.metrics_vector import (
    VECTOR_INDEXING_LAG,
    VECTOR_RECONCILE_OLDEST_LAG_SECONDS,
    VECTOR_RECONCILE_ROWS_TOTAL,
    VECTOR_RECONCILE_RUNS_TOTAL,
    VECTOR_WRITES_TOTAL,
    compute_vector_reconcile_oldest_lag_seconds,
    record_vector_index_lag,
    record_vector_reconcile_rows,
    record_vector_reconcile_run,
    record_vector_write,
    set_vector_reconcile_oldest_lag_seconds,
)

# ---------------------------------------------------------------------------
# Web-search enrichment
# ---------------------------------------------------------------------------
from app.observability.metrics_web_search import (
    WEB_SEARCH_DECISIONS_TOTAL,
    WEB_SEARCH_QUERY_RESULTS,
    record_web_search_decision,
    record_web_search_query_results,
)

# ---------------------------------------------------------------------------
# Module-level names present in original metrics.py dir() — kept for parity
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Metrics exposition helpers (use the shared REGISTRY from _metrics_base)
# ---------------------------------------------------------------------------


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


__all__ = [  # noqa: RUF022 — grouped by domain, not alphabetical
    # stdlib re-exports that appeared in original module dir()
    "Any",
    "threading",
    # logging
    "get_logger",
    "logger",
    # prometheus_client re-exports
    "CONTENT_TYPE_LATEST",
    "CollectorRegistry",
    "Counter",
    "Gauge",
    "Histogram",
    "generate_latest",
    # Base
    "PROMETHEUS_AVAILABLE",
    "REGISTRY",
    "_DEFAULT_MODEL_ALLOWLIST",
    "_KNOWN_PLATFORMS",
    "_bucket_model",
    "_bucket_platform",
    "_metric_label",
    "_model_allowlist",
    "_model_allowlist_lock",
    "_parse_failure_stage",
    "configure_model_allowlist",
    # Request / URL queue
    "REQUEST_LATENCY",
    "REQUESTS_TOTAL",
    "URL_ENQUEUE_TOTAL",
    "URL_PROCESSING_QUEUE_DEPTH",
    "URL_PROCESSOR_IN_FLIGHT",
    "record_request",
    "record_url_enqueue",
    "set_url_processing_queue_depth",
    "set_url_processor_in_flight",
    # Scraper chain
    "FIRECRAWL_LATENCY",
    "FIRECRAWL_REQUESTS",
    "SCRAPER_ATTEMPT_LATENCY_SECONDS",
    "SCRAPER_ATTEMPTS_TOTAL",
    "SCRAPER_CHAIN_ATTEMPTS_TOTAL",
    "SCRAPER_CHAIN_DURATION_SECONDS",
    "SCRAPER_CHAIN_FAILURES_TOTAL",
    "SCRAPER_CHAIN_SUCCESSES_TOTAL",
    "SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS",
    "record_firecrawl_request",
    "record_scraper_attempt",
    "record_scraper_attempt_latency",
    "record_scraper_chain_attempt",
    "record_scraper_chain_duration",
    "record_scraper_chain_failure",
    "record_scraper_chain_success",
    "record_scraper_chain_total_latency",
    # LLM / OpenRouter
    "LLM_CALL_ATTEMPTS_TOTAL",
    "LLM_CALL_LATENCY_SECONDS",
    "LLM_CALL_RETRY_EXHAUSTION_TOTAL",
    "LLM_COST_USD_TOTAL",
    "LLM_FALLBACK_ATTEMPTS_TOTAL",
    "LLM_PARSE_FAILURES_TOTAL",
    "LLM_REPAIR_ATTEMPTS_TOTAL",
    "LLM_REQUEST_SLOW_TOTAL",
    "LLM_REQUEST_TOTAL_LATENCY_SECONDS",
    "LLM_TIMEOUTS_TOTAL",
    "LLM_TOKENS_TOTAL",
    "OPENROUTER_CIRCUIT_BREAKER_STATE",
    "OPENROUTER_COST_USD",
    "OPENROUTER_LATENCY",
    "OPENROUTER_PER_MODEL_LATENCY",
    "OPENROUTER_PER_MODEL_TIMEOUT",
    "OPENROUTER_STREAM_FALLBACK",
    "OPENROUTER_TOKENS",
    "record_llm_call_attempt",
    "record_llm_call_latency",
    "record_llm_call_persisted",
    "record_llm_call_retry_exhaustion",
    "record_llm_request_total_latency",
    "record_openrouter_call",
    "record_openrouter_stream_fallback",
    "record_per_model_circuit_breaker_state",
    "record_per_model_latency",
    "record_per_model_timeout",
    # Auth / sessions
    "RATE_LIMIT_HITS_TOTAL",
    "TOKEN_FAMILY_DECISIONS_TOTAL",
    "record_rate_limit_hit",
    "record_token_family_decision",
    # Shared Redis cache
    "REDIS_CACHE_OPERATIONS_TOTAL",
    "REDIS_CACHE_OPERATION_LATENCY_SECONDS",
    "cache_namespace_family",
    "record_redis_cache_operation",
    # MCP
    "MCP_UNSCOPED_ENABLED",
    "set_mcp_unscoped_enabled",
    # Circuit breaker
    "CIRCUIT_BREAKER_STATE",
    "record_circuit_breaker_state",
    # Twitter/X + extraction pipeline
    "EXTRACTION_ATTEMPTS",
    "EXTRACTION_FAILURES",
    "EXTRACTION_STAGE_LATENCY",
    "TWITTER_ARTICLE_EXTRACTION",
    "TWITTER_ARTICLE_RESOLUTION",
    "TWITTER_ARTICLE_RESOLUTION_LATENCY",
    "record_extraction_attempt",
    "record_extraction_failure",
    "record_twitter_article_extraction",
    "record_twitter_article_resolution",
    # Web-search enrichment
    "WEB_SEARCH_DECISIONS_TOTAL",
    "WEB_SEARCH_QUERY_RESULTS",
    "record_web_search_decision",
    "record_web_search_query_results",
    # Aggregation
    "AGGREGATION_BUNDLE_LATENCY",
    "AGGREGATION_BUNDLES",
    "AGGREGATION_COST_USD",
    "AGGREGATION_EXTRACTION",
    "AGGREGATION_SYNTHESIS_COVERAGE",
    "AGGREGATION_USED_SOURCES",
    "record_aggregation_bundle",
    "record_aggregation_extraction",
    "record_aggregation_synthesis",
    # Streaming
    "DRAFT_STREAM_EVENTS",
    "STREAM_LATENCY_MS",
    "record_draft_stream_event",
    "record_stream_latency_ms",
    # Social
    "SOCIAL_CONNECTION_STATUS_TOTAL",
    "SOCIAL_FETCH_TOTAL",
    "SOCIAL_RATE_LIMIT_TOTAL",
    "SOCIAL_TOKEN_REFRESH_TOTAL",
    "record_social_connection_status",
    "record_social_fetch",
    "record_social_rate_limit",
    "record_social_token_refresh",
    # Database + admin diagnostics
    "ADMIN_DIAGNOSTICS_REQUESTS",
    "DB_CONNECTIONS",
    "DB_QUERY_LATENCY",
    "record_admin_diagnostics_request",
    "record_db_query",
    "set_db_connections",
    # Telegram channel digest
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
    # Speech-to-text
    "STT_AUDIO_SECONDS_TOTAL",
    "STT_REQUESTS_TOTAL",
    "record_stt_audio_seconds",
    "record_stt_request",
    # Text-to-speech
    "TTS_AUDIO_BYTES_TOTAL",
    "TTS_LATENCY_SECONDS",
    "TTS_REQUESTS_TOTAL",
    "record_tts_audio_bytes",
    "record_tts_latency",
    "record_tts_request",
    # Vector store
    "VECTOR_INDEXING_LAG",
    "VECTOR_RECONCILE_OLDEST_LAG_SECONDS",
    "VECTOR_RECONCILE_ROWS_TOTAL",
    "VECTOR_RECONCILE_RUNS_TOTAL",
    "VECTOR_WRITES_TOTAL",
    "compute_vector_reconcile_oldest_lag_seconds",
    "record_vector_index_lag",
    "record_vector_reconcile_rows",
    "record_vector_reconcile_run",
    "record_vector_write",
    "set_vector_reconcile_oldest_lag_seconds",
    # Scheduler
    "SCHEDULER_JOB_CHRONIC_FAILURES",
    "SCHEDULER_QUEUE_DEPTH",
    "TASKIQ_RETRIES_TOTAL",
    "record_scheduler_chronic_failure",
    "record_taskiq_retry_outcome",
    "set_scheduler_queue_depth",
    # Exposition helpers
    "get_metrics",
    "get_metrics_content_type",
]
