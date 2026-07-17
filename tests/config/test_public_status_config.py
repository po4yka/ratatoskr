from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.deployment import DeploymentConfig


def test_public_status_config_has_bounded_defaults() -> None:
    config = DeploymentConfig()

    assert config.status_bot_metrics_url is None
    assert config.status_worker_metrics_url is None
    assert config.status_scheduler_metrics_url is None
    assert config.status_node_metrics_url is None
    assert 0 < config.status_probe_timeout_seconds <= config.status_total_timeout_seconds <= 5
    assert 15 <= config.status_cache_ttl_seconds <= 30


@pytest.mark.parametrize(
    "url",
    [
        "https://bot:9101/metrics",
        "http://user:secret@bot:9101/metrics",
        "http://bot:9101/metrics?token=secret",
        "http://bot:9101/metrics#fragment",
        "http:///metrics",
        "not-a-url",
    ],
)
def test_public_status_config_rejects_unsafe_probe_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        DeploymentConfig(STATUS_BOT_METRICS_URL=url)


def test_public_status_config_accepts_internal_http_probe_url() -> None:
    config = DeploymentConfig(
        STATUS_BOT_METRICS_URL="http://bot:9101/metrics",
        STATUS_NODE_METRICS_URL="http://node-exporter:9100/metrics",
    )

    assert config.status_bot_metrics_url == "http://bot:9101/metrics"
    assert config.status_node_metrics_url == "http://node-exporter:9100/metrics"


def test_public_status_config_rejects_probe_timeout_above_total() -> None:
    with pytest.raises(ValidationError):
        DeploymentConfig(
            STATUS_PROBE_TIMEOUT_SECONDS=3,
            STATUS_TOTAL_TIMEOUT_SECONDS=2,
        )
