"""Pure budget evaluation for LLM usage."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.core.time_utils import UTC


@dataclass(frozen=True)
class LLMUsageSnapshot:
    daily_cost_usd: float = 0.0
    monthly_cost_usd: float = 0.0


@dataclass(frozen=True)
class LLMUsageBudgetDecision:
    allowed: bool
    hard_stop: bool = False
    warning: bool = False
    reasons: tuple[str, ...] = ()
    status: str = "ok"


def day_start(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def total_tokens(prompt_tokens: int | None, completion_tokens: int | None) -> int:
    return int(prompt_tokens or 0) + int(completion_tokens or 0)


def evaluate_request_usage(
    *,
    budget: Any,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: float | None,
) -> LLMUsageBudgetDecision:
    """Evaluate per-call usage after a provider result is known."""
    reasons: list[str] = []
    max_tokens = getattr(budget, "max_tokens_per_request", None)
    max_cost = getattr(budget, "max_cost_usd_per_request", None)
    tokens = total_tokens(prompt_tokens, completion_tokens)

    if max_tokens is not None and tokens > int(max_tokens):
        reasons.append("request_tokens_exceeded")
    if max_cost is not None and cost_usd is not None and float(cost_usd) > float(max_cost):
        reasons.append("request_cost_exceeded")

    return LLMUsageBudgetDecision(
        allowed=not reasons,
        hard_stop=bool(reasons),
        reasons=tuple(reasons),
        status="hard_stop" if reasons else "ok",
    )


def evaluate_aggregate_budget(
    *,
    budget: Any,
    usage: LLMUsageSnapshot,
) -> LLMUsageBudgetDecision:
    """Evaluate aggregate soft and hard cost budgets before a new LLM call."""
    reasons: list[str] = []
    warnings: list[str] = []
    threshold = float(getattr(budget, "warning_threshold_ratio", 0.8) or 0.8)

    daily_hard = getattr(budget, "daily_hard_budget_usd", None)
    monthly_hard = getattr(budget, "monthly_hard_budget_usd", None)
    daily_soft = getattr(budget, "daily_soft_budget_usd", None)
    monthly_soft = getattr(budget, "monthly_soft_budget_usd", None)

    if daily_hard is not None and usage.daily_cost_usd >= float(daily_hard):
        reasons.append("daily_hard_budget_exceeded")
    if monthly_hard is not None and usage.monthly_cost_usd >= float(monthly_hard):
        reasons.append("monthly_hard_budget_exceeded")
    if daily_soft is not None and usage.daily_cost_usd >= float(daily_soft) * threshold:
        warnings.append("daily_soft_budget_warning")
    if monthly_soft is not None and usage.monthly_cost_usd >= float(monthly_soft) * threshold:
        warnings.append("monthly_soft_budget_warning")

    all_reasons = tuple(reasons or warnings)
    if reasons:
        status = "hard_stop"
    elif warnings:
        status = "warning"
    else:
        status = "ok"
    return LLMUsageBudgetDecision(
        allowed=not reasons,
        hard_stop=bool(reasons),
        warning=bool(warnings),
        reasons=all_reasons,
        status=status,
    )
