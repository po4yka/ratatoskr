from __future__ import annotations

import pytest

from app.adapters.content.llm_call_budget import LLMCallBudget, LLMCallCapExceeded


def test_budget_charges_up_to_limit_then_raises() -> None:
    budget = LLMCallBudget(3)
    assert budget.limit == 3
    assert budget.charge() == 1
    assert budget.charge() == 2
    assert budget.charge() == 3
    assert budget.count == 3
    assert budget.remaining() == 0
    with pytest.raises(LLMCallCapExceeded):
        budget.charge()
    # A rejected charge does not increment the count.
    assert budget.count == 3


def test_would_exceed_reflects_remaining_capacity() -> None:
    budget = LLMCallBudget(1)
    assert budget.would_exceed() is False
    budget.charge()
    assert budget.would_exceed() is True
    assert budget.remaining() == 0


def test_limit_is_floored_to_one() -> None:
    budget = LLMCallBudget(0)
    assert budget.limit == 1
    budget.charge()
    with pytest.raises(LLMCallCapExceeded):
        budget.charge()


def test_cap_exceeded_carries_limit() -> None:
    budget = LLMCallBudget(2)
    budget.charge()
    budget.charge()
    with pytest.raises(LLMCallCapExceeded) as exc_info:
        budget.charge()
    assert exc_info.value.limit == 2
