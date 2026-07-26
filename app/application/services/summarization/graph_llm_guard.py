"""Shared execution guard for every summarize-graph LLM provider call."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from app.core.llm_call_budget import LLMCallCapExceeded
from app.core.llm_usage_budget import (
    LLMUsageSnapshot,
    day_start,
    evaluate_aggregate_budget,
    evaluate_request_usage,
    month_start,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.application.ports.requests import LLMRepositoryPort

logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT")


class GraphLLMConcurrencyTimeout(TimeoutError):
    """Raised when the graph cannot acquire the shared LLM semaphore in time."""


class GraphLLMUsageBudgetExceeded(RuntimeError):
    """Raised before a provider call when an aggregate hard budget is exhausted."""


@dataclass(frozen=True, slots=True)
class GraphLLMGuardConfig:
    semaphore_acquire_timeout_sec: float = 30.0
    call_timeout_sec: float = 420.0
    max_calls_per_request: int = 8
    max_tokens_per_request: int | None = None
    max_cost_usd_per_request: float | None = None
    daily_soft_budget_usd: float | None = None
    monthly_soft_budget_usd: float | None = None
    daily_hard_budget_usd: float | None = None
    monthly_hard_budget_usd: float | None = None
    warning_threshold_ratio: float = 0.8


class GraphLLMGuard:
    """Apply concurrency, timeout, usage-budget, and call-cap policy uniformly."""

    def __init__(
        self,
        *,
        sem: Callable[[], Any],
        llm_repo: LLMRepositoryPort | None,
        config: GraphLLMGuardConfig,
    ) -> None:
        self._sem = sem
        self._llm_repo = llm_repo
        self._config = config

    async def invoke(
        self,
        *,
        current_call_count: int,
        request_id: int | None,
        model: str | None,
        max_tokens: int | None,
        call: Callable[[int | None], Awaitable[_ResultT]],
    ) -> tuple[_ResultT, int]:
        """Run one provider invocation through all graph execution guards."""
        call_count = max(0, int(current_call_count))
        if call_count >= self._config.max_calls_per_request:
            raise LLMCallCapExceeded(self._config.max_calls_per_request)

        effective_max_tokens = max_tokens
        configured_max_tokens = self._config.max_tokens_per_request
        if configured_max_tokens is not None:
            effective_max_tokens = (
                configured_max_tokens
                if effective_max_tokens is None
                else min(int(effective_max_tokens), configured_max_tokens)
            )

        await self._check_aggregate_budget(request_id=request_id)

        semaphore = self._sem()
        try:
            async with asyncio.timeout(self._config.semaphore_acquire_timeout_sec):
                await semaphore.__aenter__()
        except TimeoutError:
            raise GraphLLMConcurrencyTimeout(
                "timed out acquiring the shared LLM concurrency slot"
            ) from None

        next_call_count = call_count + 1
        try:
            async with asyncio.timeout(self._config.call_timeout_sec):
                result = await call(effective_max_tokens)
        except BaseException as exc:
            _attach_call_count(exc, next_call_count)
            raise
        finally:
            await semaphore.__aexit__(None, None, None)

        self._report_request_budget(result=result, request_id=request_id, model=model)
        return result, next_call_count

    async def _check_aggregate_budget(self, *, request_id: int | None) -> None:
        if self._llm_repo is None:
            return
        try:
            usage = LLMUsageSnapshot(
                daily_cost_usd=await self._llm_repo.async_get_cost_usd_since(day_start()),
                monthly_cost_usd=await self._llm_repo.async_get_cost_usd_since(month_start()),
            )
        except Exception:
            logger.warning(
                "graph_llm_budget_lookup_failed",
                extra={"request_id": request_id},
                exc_info=True,
            )
            return

        decision = evaluate_aggregate_budget(budget=self._config, usage=usage)
        if decision.warning:
            logger.warning(
                "graph_llm_budget_warning",
                extra={
                    "request_id": request_id,
                    "status": decision.status,
                    "reasons": list(decision.reasons),
                },
            )
        if not decision.allowed:
            raise GraphLLMUsageBudgetExceeded(
                f"LLM usage budget hard stop: {', '.join(decision.reasons)}"
            )

    def _report_request_budget(
        self,
        *,
        result: Any,
        request_id: int | None,
        model: str | None,
    ) -> None:
        decision = evaluate_request_usage(
            budget=self._config,
            prompt_tokens=getattr(result, "tokens_prompt", None),
            completion_tokens=getattr(result, "tokens_completion", None),
            cost_usd=getattr(result, "cost_usd", None),
        )
        if decision.reasons:
            logger.warning(
                "graph_llm_request_budget_exceeded",
                extra={
                    "request_id": request_id,
                    "model": model,
                    "status": decision.status,
                    "reasons": list(decision.reasons),
                },
            )


def _attach_call_count(exc: BaseException, call_count: int) -> None:
    try:
        exc.__dict__["__llm_call_count__"] = call_count
    except Exception:
        return
