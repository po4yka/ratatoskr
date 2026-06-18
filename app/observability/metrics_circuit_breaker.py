"""Prometheus metrics for the generic (service-level) circuit breaker.

The per-model OpenRouter circuit breaker lives in metrics_llm.py alongside
the other OpenRouter metrics.  This module covers the coarser service-level
breaker used for Firecrawl, etc.
"""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Gauge

    CIRCUIT_BREAKER_STATE = Gauge(
        "ratatoskr_circuit_breaker_state",
        "Circuit breaker state (0=closed, 1=half_open, 2=open)",
        ["service"],
        registry=REGISTRY,
    )

else:
    CIRCUIT_BREAKER_STATE = None


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
