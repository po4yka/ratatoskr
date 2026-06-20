from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.observability import metrics, metrics_auth


def _counter_value(exported: str, decision: str) -> float:
    match = re.search(
        rf'^ratatoskr_token_family_decisions_total{{decision="{decision}"}} ([0-9.]+)$',
        exported,
        re.MULTILINE,
    )
    return float(match.group(1)) if match else 0.0


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_token_family_decision_counter_exports_all_policy_decisions() -> None:
    for decision in ("rotate", "reject", "revoke_family"):
        metrics.record_token_family_decision(decision)

    exported = metrics.get_metrics().decode("utf-8")

    assert 'ratatoskr_token_family_decisions_total{decision="rotate"}' in exported
    assert 'ratatoskr_token_family_decisions_total{decision="reject"}' in exported
    assert 'ratatoskr_token_family_decisions_total{decision="revoke_family"}' in exported


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_token_family_revocation_counter_increments_by_one() -> None:
    before = _counter_value(metrics.get_metrics().decode("utf-8"), "revoke_family")

    metrics.record_token_family_decision("revoke_family")

    after = _counter_value(metrics.get_metrics().decode("utf-8"), "revoke_family")
    assert after == before + 1


def test_token_family_metric_helpers_are_noops_without_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics_auth, "PROMETHEUS_AVAILABLE", False)

    metrics.record_token_family_decision("revoke_family")


def test_token_family_revocation_alert_is_critical() -> None:
    rule_text = Path("ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8")

    assert "RatatoskrTokenFamilyRevocationDetected" in rule_text
    assert 'ratatoskr_token_family_decisions_total{decision="revoke_family"}' in rule_text
    assert "severity: critical" in rule_text
    assert "Possible credential theft — family revocation detected" in rule_text
