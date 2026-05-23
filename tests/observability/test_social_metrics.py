from __future__ import annotations

import pytest

from app.observability import metrics


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_social_metrics_are_exported() -> None:
    metrics.record_social_fetch(provider="X", status="succeeded", auth_tier="x_api")
    metrics.record_social_token_refresh(provider="threads", status="failed")
    metrics.record_social_rate_limit(provider="instagram")
    metrics.record_social_connection_status(provider="x", status="active")

    exported = metrics.get_metrics().decode("utf-8")

    assert (
        'ratatoskr_social_fetch_total{auth_tier="x_api",provider="x",status="succeeded"}'
        in exported
    )
    assert 'ratatoskr_social_token_refresh_total{provider="threads",status="failed"}' in exported
    assert 'ratatoskr_social_rate_limit_total{provider="instagram"}' in exported
    assert 'ratatoskr_social_connection_status_total{provider="x",status="active"}' in exported


def test_social_metric_helpers_are_noops_without_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics, "PROMETHEUS_AVAILABLE", False)

    metrics.record_social_fetch(provider="x", status="failed", auth_tier="x_api")
    metrics.record_social_token_refresh(provider="x", status="failed")
    metrics.record_social_rate_limit(provider="x")
    metrics.record_social_connection_status(provider="x", status="needs_reauth")
