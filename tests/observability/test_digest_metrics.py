from __future__ import annotations

import pytest

from app.observability import metrics


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_digest_metrics_are_exported_and_increment() -> None:
    registry = metrics.REGISTRY
    assert registry is not None

    before_fetch_errors = (
        registry.get_sample_value(
            "ratatoskr_digest_channel_fetch_errors_total",
            {"reason": "fetch_failed"},
        )
        or 0.0
    )
    before_histogram = (
        registry.get_sample_value(
            "ratatoskr_digest_pipeline_duration_seconds_count",
            {"digest_type": "scheduled", "status": "sent"},
        )
        or 0.0
    )

    metrics.record_digest_channel_fetch_error("fetch_failed")
    metrics.record_digest_pipeline_duration(
        digest_type="scheduled",
        status="sent",
        duration_seconds=1.25,
    )
    metrics.set_digest_active_subscription_users(3)

    assert (
        registry.get_sample_value(
            "ratatoskr_digest_channel_fetch_errors_total",
            {"reason": "fetch_failed"},
        )
        or 0.0
    ) - before_fetch_errors == pytest.approx(1.0)
    assert (
        registry.get_sample_value(
            "ratatoskr_digest_pipeline_duration_seconds_count",
            {"digest_type": "scheduled", "status": "sent"},
        )
        or 0.0
    ) - before_histogram == pytest.approx(1.0)
    assert registry.get_sample_value("ratatoskr_digest_active_subscription_users") == pytest.approx(
        3.0
    )

    exported = metrics.get_metrics().decode("utf-8")
    assert "ratatoskr_digest_deliveries_total" in exported
    assert "ratatoskr_digest_posts_analyzed_total" in exported
    assert "ratatoskr_digest_userbot_reconnects_total" in exported
