from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services.summarization.graph_llm_guard import (
    GraphLLMGuard,
    GraphLLMGuardConfig,
    GraphLLMUsageBudgetExceeded,
)
from app.core.llm_call_budget import LLMCallCapExceeded


def _guard(
    *,
    config: GraphLLMGuardConfig | None = None,
    repo: object | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> GraphLLMGuard:
    shared = semaphore or asyncio.Semaphore(1)
    return GraphLLMGuard(
        sem=lambda: shared,
        llm_repo=repo,  # type: ignore[arg-type]
        config=config or GraphLLMGuardConfig(),
    )


async def test_guard_clamps_tokens_and_counts_provider_invocation() -> None:
    seen: list[int | None] = []
    guard = _guard(config=GraphLLMGuardConfig(max_tokens_per_request=100))

    async def call(max_tokens: int | None) -> object:
        seen.append(max_tokens)
        return SimpleNamespace(tokens_prompt=10, tokens_completion=20, cost_usd=0.01)

    _result, count = await guard.invoke(
        current_call_count=2,
        request_id=7,
        model="m",
        max_tokens=500,
        call=call,
    )

    assert seen == [100]
    assert count == 3


async def test_guard_enforces_per_request_call_cap() -> None:
    guard = _guard(config=GraphLLMGuardConfig(max_calls_per_request=2))

    with pytest.raises(LLMCallCapExceeded):
        await guard.invoke(
            current_call_count=2,
            request_id=7,
            model="m",
            max_tokens=None,
            call=AsyncMock(),
        )


async def test_guard_enforces_call_timeout_and_releases_semaphore() -> None:
    semaphore = asyncio.Semaphore(1)
    guard = _guard(
        config=GraphLLMGuardConfig(call_timeout_sec=0.01),
        semaphore=semaphore,
    )

    async def blocked(_max_tokens: int | None) -> object:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as raised:
        await guard.invoke(
            current_call_count=0,
            request_id=7,
            model="m",
            max_tokens=None,
            call=blocked,
        )

    assert raised.value.__dict__["__llm_call_count__"] == 1
    assert not semaphore.locked()


async def test_guard_uses_one_global_semaphore() -> None:
    semaphore = asyncio.Semaphore(1)
    guard = _guard(semaphore=semaphore)
    active = 0
    max_active = 0

    async def call(_max_tokens: int | None) -> object:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(tokens_prompt=0, tokens_completion=0, cost_usd=0.0)

    await asyncio.gather(
        guard.invoke(
            current_call_count=0,
            request_id=1,
            model="m",
            max_tokens=None,
            call=call,
        ),
        guard.invoke(
            current_call_count=0,
            request_id=2,
            model="m",
            max_tokens=None,
            call=call,
        ),
    )

    assert max_active == 1


async def test_guard_stops_before_call_at_aggregate_hard_budget() -> None:
    repo = SimpleNamespace(async_get_cost_usd_since=AsyncMock(return_value=5.0))
    guard = _guard(
        config=GraphLLMGuardConfig(daily_hard_budget_usd=5.0),
        repo=repo,
    )
    call = AsyncMock()

    with pytest.raises(GraphLLMUsageBudgetExceeded):
        await guard.invoke(
            current_call_count=0,
            request_id=7,
            model="m",
            max_tokens=None,
            call=call,
        )

    call.assert_not_awaited()
