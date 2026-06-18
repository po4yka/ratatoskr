"""Shared base for all metrics submodules.

Provides the optional prometheus_client import guard, the shared REGISTRY,
and label-bucketing utilities consumed by every domain metrics module.

This module must be imported before any submodule registers metrics so that
the REGISTRY singleton is created exactly once.  All submodules do:

    from app.observability._metrics_base import (
        PROMETHEUS_AVAILABLE,
        REGISTRY,
        _bucket_model,
        _bucket_platform,
        _metric_label,
        _parse_failure_stage,
        configure_model_allowlist,
    )
"""

from __future__ import annotations

import threading
from typing import Any

from app.core.logging_utils import get_logger

# ---------------------------------------------------------------------------
# Optional prometheus_client import — fail-open sentinel pattern
# ---------------------------------------------------------------------------
try:
    from prometheus_client import CollectorRegistry

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared registry — created once; all submodules register into it
# ---------------------------------------------------------------------------
REGISTRY = CollectorRegistry() if PROMETHEUS_AVAILABLE else None

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


def _metric_label(value: Any) -> str:
    label = str(value or "unknown").strip().lower()
    return label or "unknown"


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
