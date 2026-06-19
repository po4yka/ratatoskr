from __future__ import annotations

from pathlib import Path


def test_github_sync_alert_rules_cover_rate_limits_and_budget_caps() -> None:
    rule_text = Path("ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8")

    assert "RatatoskrGitHubSyncRateLimitedConsecutively" in rule_text
    assert "ratatoskr_github_sync_rate_limit_streak > 3" in rule_text
    assert "RatatoskrGitHubSyncLLMBudgetCapHigh" in rule_text
    assert 'ratatoskr_github_sync_llm_calls_total{trigger="deferred"}' in rule_text
    assert 'ratatoskr_github_sync_llm_calls_total{trigger="made"}' in rule_text
    assert "severity: warning" in rule_text
