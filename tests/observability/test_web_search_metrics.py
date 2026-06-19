from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.web_search_agent import SearchAnalysisResult, WebSearchAgent, WebSearchAgentInput
from app.observability import metrics


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_web_search_decision_metrics_increment_all_decision_kinds() -> None:
    registry = metrics.REGISTRY
    assert registry is not None

    for decision in ("executed", "skipped_low_value", "skipped_disabled", "failed"):
        before = (
            registry.get_sample_value(
                "ratatoskr_web_search_decisions_total",
                {"decision": decision},
            )
            or 0.0
        )
        metrics.record_web_search_decision(decision)
        after = registry.get_sample_value(
            "ratatoskr_web_search_decisions_total",
            {"decision": decision},
        )
        assert after == pytest.approx(before + 1.0)


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_web_search_query_results_histogram_observes_result_count() -> None:
    registry = metrics.REGISTRY
    assert registry is not None

    before = registry.get_sample_value("ratatoskr_web_search_query_results_count") or 0.0
    metrics.record_web_search_query_results(3)
    after = registry.get_sample_value("ratatoskr_web_search_query_results_count")
    assert after == pytest.approx(before + 1.0)


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_openrouter_cost_metric_has_web_search_purpose_label() -> None:
    registry = metrics.REGISTRY
    assert registry is not None

    before = (
        registry.get_sample_value(
            "ratatoskr_openrouter_cost_usd_total",
            {"purpose": "web_search"},
        )
        or 0.0
    )
    metrics.record_openrouter_call(
        model="unknown/web-search-model",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.25,
        purpose="web_search",
    )
    after = registry.get_sample_value(
        "ratatoskr_openrouter_cost_usd_total",
        {"purpose": "web_search"},
    )
    assert after == pytest.approx(before + 0.25)


@pytest.mark.asyncio
async def test_web_search_agent_records_executed_decision_and_query_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions: list[str] = []
    result_counts: list[int] = []

    monkeypatch.setattr(
        "app.agents.web_search_agent.record_web_search_decision",
        decisions.append,
    )
    monkeypatch.setattr(
        "app.agents.web_search_agent.record_web_search_query_results",
        result_counts.append,
    )
    monkeypatch.setattr(
        "app.agents.web_search_agent.record_openrouter_call",
        lambda **_: None,
    )

    llm = SimpleNamespace(
        _model="test/model",
        chat_structured=AsyncMock(
            return_value=SimpleNamespace(
                parsed=SearchAnalysisResult(
                    needs_search=True,
                    queries=["ratatoskr web search"],
                    reason="needs current context",
                ),
                tokens_prompt=10,
                tokens_completion=5,
                cost_usd=0.001,
                latency_ms=250,
                model_used="test/model",
            )
        ),
    )
    search = SimpleNamespace(
        find_articles=AsyncMock(
            return_value=[
                SimpleNamespace(
                    title="Article",
                    snippet="Snippet",
                    url="https://example.com",
                    source="Example",
                    published_at="2026-06-19",
                )
            ]
        )
    )
    cfg = SimpleNamespace(min_content_length=10, max_context_chars=1000, max_queries=3)
    agent = WebSearchAgent(llm_client=llm, search_service=search, cfg=cfg)

    result = await agent.execute(WebSearchAgentInput(content="A" * 100, language="en"))

    assert result.success is True
    assert decisions == ["executed"]
    assert result_counts == [1]


def test_web_search_cost_regression_alert_is_registered() -> None:
    rule_text = Path("ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8")

    assert "RatatoskrWebSearchEnrichmentCostRegression" in rule_text
    assert 'ratatoskr_web_search_decisions_total{decision="executed"}' in rule_text
    assert 'purpose=\\"web_search\\"' in rule_text
    assert "for: 30m" in rule_text
