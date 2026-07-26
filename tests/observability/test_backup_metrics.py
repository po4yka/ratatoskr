from __future__ import annotations

import pytest

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY
from app.observability.metrics_backup import record_backup_run, set_backup_items

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client unavailable")


def _sample(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return float(value or 0)


def test_backup_run_metric_preserves_partial_outcome() -> None:
    labels = {"backup": "github_repositories", "outcome": "partial"}
    before = _sample("ratatoskr_backup_runs_total", labels)

    record_backup_run("github_repositories", "partial")

    assert _sample("ratatoskr_backup_runs_total", labels) == before + 1


def test_backup_metric_buckets_untrusted_labels() -> None:
    record_backup_run("private/repository", "cookie-secret")

    assert _sample("ratatoskr_backup_runs_total", {"backup": "unknown", "outcome": "unknown"}) >= 1
    assert (
        REGISTRY.get_sample_value(
            "ratatoskr_backup_runs_total",
            {"backup": "private/repository", "outcome": "cookie-secret"},
        )
        is None
    )


def test_backup_item_metric_clamps_negative_counts() -> None:
    set_backup_items("github_repositories", ok=3, failed=1, skipped=-2)

    assert _sample("ratatoskr_backup_items", {"backup": "github_repositories", "result": "ok"}) == 3
    assert (
        _sample("ratatoskr_backup_items", {"backup": "github_repositories", "result": "failed"})
        == 1
    )
    assert (
        _sample("ratatoskr_backup_items", {"backup": "github_repositories", "result": "skipped"})
        == 0
    )
