from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.observability import metrics_source_content
from app.observability.metrics_taskiq import bucket_taskiq_task


def test_source_artifact_violation_counter_uses_bounded_stage() -> None:
    metric = metrics_source_content.SOURCE_ARTIFACT_INVARIANT_VIOLATIONS_TOTAL
    if not metrics_source_content.PROMETHEUS_AVAILABLE or metric is None:
        pytest.skip("prometheus_client unavailable")

    before = metric.labels(stage="other")._value.get()
    metrics_source_content.record_source_artifact_invariant_violation(
        stage="request-id-from-user-input"
    )
    assert metric.labels(stage="other")._value.get() == before + 1


def test_source_artifact_violation_has_critical_alert() -> None:
    rules = yaml.safe_load(Path("ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8"))
    alerts = {
        rule["alert"]: rule
        for group in rules["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }
    alert = alerts["RatatoskrSourceArtifactInvariantViolation"]
    assert alert["expr"] == "increase(ratatoskr_source_artifact_invariant_violations_total[5m]) > 0"
    assert alert["labels"]["severity"] == "critical"
    assert alerts["RatatoskrSourceContentBacklog"]["expr"] == (
        "ratatoskr_source_content_missing > 0"
    )
    stopped = alerts["RatatoskrSourceContentReconcilerStopped"]
    assert "ratatoskr_source_content_reconcile_runs_total" in stopped["expr"]
    assert stopped["labels"]["severity"] == "critical"


def test_source_content_reconcile_has_bounded_taskiq_label() -> None:
    assert (
        bucket_taskiq_task("ratatoskr.source_content.reconcile")
        == "ratatoskr.source_content.reconcile"
    )


def test_oldest_missing_age_gauge_clamps_negative_values() -> None:
    metric = metrics_source_content.SOURCE_CONTENT_OLDEST_MISSING_AGE_SECONDS
    if not metrics_source_content.PROMETHEUS_AVAILABLE or metric is None:
        pytest.skip("prometheus_client unavailable")

    metrics_source_content.set_source_content_oldest_missing_age_seconds(-1)

    assert metric._value.get() == 0
