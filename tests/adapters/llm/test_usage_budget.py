from __future__ import annotations

from types import SimpleNamespace

from app.adapters.llm.usage_budget import (
    LLMUsageSnapshot,
    evaluate_aggregate_budget,
    evaluate_request_usage,
)


def _budget(**kwargs):
    defaults = {
        "max_tokens_per_request": None,
        "max_cost_usd_per_request": None,
        "daily_soft_budget_usd": None,
        "monthly_soft_budget_usd": None,
        "warning_threshold_ratio": 0.8,
        "daily_hard_budget_usd": None,
        "monthly_hard_budget_usd": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_request_budget_allows_missing_cost_data() -> None:
    decision = evaluate_request_usage(
        budget=_budget(max_cost_usd_per_request=0.01),
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=None,
    )

    assert decision.allowed is True
    assert decision.reasons == ()


def test_request_budget_blocks_token_and_cost_overages() -> None:
    decision = evaluate_request_usage(
        budget=_budget(max_tokens_per_request=100, max_cost_usd_per_request=0.01),
        prompt_tokens=80,
        completion_tokens=30,
        cost_usd=0.02,
    )

    assert decision.allowed is False
    assert decision.hard_stop is True
    assert decision.reasons == ("request_tokens_exceeded", "request_cost_exceeded")


def test_aggregate_budget_warns_at_soft_threshold() -> None:
    decision = evaluate_aggregate_budget(
        budget=_budget(daily_soft_budget_usd=10.0, warning_threshold_ratio=0.75),
        usage=LLMUsageSnapshot(daily_cost_usd=7.5, monthly_cost_usd=0.0),
    )

    assert decision.allowed is True
    assert decision.warning is True
    assert decision.status == "warning"
    assert decision.reasons == ("daily_soft_budget_warning",)


def test_aggregate_budget_blocks_hard_stop() -> None:
    decision = evaluate_aggregate_budget(
        budget=_budget(monthly_hard_budget_usd=50.0),
        usage=LLMUsageSnapshot(daily_cost_usd=1.0, monthly_cost_usd=50.0),
    )

    assert decision.allowed is False
    assert decision.hard_stop is True
    assert decision.status == "hard_stop"
    assert decision.reasons == ("monthly_hard_budget_exceeded",)
