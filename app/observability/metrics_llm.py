"""Prometheus metrics for LLM and OpenRouter calls.

Covers:
- OpenRouter token usage, cost, latency (OPENROUTER_*)
- LLM retry-budget telemetry (LLM_CALL_ATTEMPTS_TOTAL, LLM_CALL_RETRY_EXHAUSTION_TOTAL, ...)
- Per-request total LLM wall-time (LLM_REQUEST_TOTAL_LATENCY_SECONDS, LLM_REQUEST_SLOW_TOTAL)
- Per-model latency and timeout (OPENROUTER_PER_MODEL_LATENCY, OPENROUTER_PER_MODEL_TIMEOUT)
- OpenRouter circuit breaker state (OPENROUTER_CIRCUIT_BREAKER_STATE)
- OpenRouter stream fallback (OPENROUTER_STREAM_FALLBACK)
"""

from __future__ import annotations

from typing import Any

from app.observability._metrics_base import (
    PROMETHEUS_AVAILABLE,
    REGISTRY,
    _bucket_model,
    _metric_label,
    _parse_failure_stage,
)

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

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
    # exhausted without success.
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
    # request (sum of llm_calls.latency_ms for the request).
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

else:
    OPENROUTER_TOKENS = None
    OPENROUTER_COST_USD = None
    OPENROUTER_LATENCY = None
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
    OPENROUTER_STREAM_FALLBACK = None
    OPENROUTER_PER_MODEL_TIMEOUT = None
    OPENROUTER_PER_MODEL_LATENCY = None
    OPENROUTER_CIRCUIT_BREAKER_STATE = None


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
