from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.observability import metrics_source_content


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
