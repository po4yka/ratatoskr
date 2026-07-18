"""Prometheus metrics for MCP exposure posture."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Gauge

    MCP_UNSCOPED_ENABLED = Gauge(
        "ratatoskr_mcp_unscoped_enabled",
        "Whether MCP SSE is running without request auth or startup user scope (0/1)",
        ["app_env"],
        multiprocess_mode="max",
        registry=REGISTRY,
    )

else:
    MCP_UNSCOPED_ENABLED = None


def set_mcp_unscoped_enabled(*, enabled: bool, app_env: str) -> None:
    """Record whether this MCP process is running in unscoped SSE mode."""
    if not PROMETHEUS_AVAILABLE or MCP_UNSCOPED_ENABLED is None:
        return
    MCP_UNSCOPED_ENABLED.labels(app_env=_metric_label(app_env)).set(1 if enabled else 0)
