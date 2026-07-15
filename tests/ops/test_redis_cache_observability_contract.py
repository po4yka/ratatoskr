from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_performance_guide_uses_supported_cache_settings() -> None:
    guide = (ROOT / "docs/guides/optimize-performance.md").read_text(encoding="utf-8")

    for supported in (
        "REDIS_CACHE_ENABLED",
        "REDIS_CACHE_TIMEOUT_SEC",
        "REDIS_FIRECRAWL_TTL_SECONDS",
        "REDIS_LLM_TTL_SECONDS",
    ):
        assert supported in guide
    for unsupported in (
        "ENABLE_FIRECRAWL_CACHE",
        "ENABLE_LLM_CACHE",
        "REDIS_MAX_CONNECTIONS",
        "TOKEN_COUNTING_MODE",
    ):
        assert unsupported not in guide


def test_overview_dashboard_exposes_cache_health_without_sensitive_labels() -> None:
    dashboard_path = ROOT / "ops/monitoring/grafana/provisioning/dashboards/ratatoskr-overview.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    cache_panels = [
        panel for panel in dashboard["panels"] if panel.get("title", "").startswith("Redis Cache")
    ]
    assert {panel["title"] for panel in cache_panels} >= {
        "Redis Cache",
        "Redis Cache Hit Ratio",
        "Redis Cache Error Rate",
        "Redis Cache Operations",
        "Redis Cache Latency p95",
    }

    expressions = "\n".join(
        target["expr"] for panel in cache_panels for target in panel.get("targets", [])
    )
    assert "ratatoskr_redis_cache_operations_total" in expressions
    assert "ratatoskr_redis_cache_operation_latency_seconds_bucket" in expressions
    assert "user_id" not in expressions
    assert "key=" not in expressions
