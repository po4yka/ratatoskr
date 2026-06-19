from __future__ import annotations

from pathlib import Path


def test_tts_alert_rules_cover_quota_and_http_error_rate() -> None:
    rule_text = Path("ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8")

    assert "RatatoskrTTSQuotaExceeded" in rule_text
    assert 'ratatoskr_tts_requests_total{outcome="quota_exceeded"}' in rule_text
    assert "for: 0m" in rule_text
    assert "RatatoskrTTSHTTPErrorRateHigh" in rule_text
    assert 'ratatoskr_tts_requests_total{outcome="http_error"}' in rule_text
    assert 'ratatoskr_tts_requests_total{outcome!="retry"}' in rule_text
    assert "> 0.05" in rule_text
