"""Tests for the provider-rotation tracker.

When OpenRouter returns a provider-specific 400 (content policy,
quota, format incompatibility), the current code advances to the
next *model* in the fallback chain even though another OpenRouter
provider serving the *same* model would likely accept the identical
request. ProviderRotationTracker keeps a per-request, per-model set
of tried providers and decides whether to rotate provider vs.
advance model, capped by ``max_rotations_per_model``.
"""

from __future__ import annotations

import pytest

from app.core.provider_rotation import ProviderRotationTracker


class TestFirstRejectionRotatesProvider:
    def test_first_rejection_returns_rotate_decision(self) -> None:
        tracker = ProviderRotationTracker(max_rotations_per_model=2)
        decision = tracker.on_provider_rejection(
            model="anthropic/claude-3-haiku", rejected_provider="Azure"
        )
        assert decision.advance_model is False
        assert decision.excluded_providers == ("Azure",)

    def test_second_rejection_accumulates_excluded(self) -> None:
        tracker = ProviderRotationTracker(max_rotations_per_model=3)
        tracker.on_provider_rejection(model="m", rejected_provider="Azure")
        decision = tracker.on_provider_rejection(model="m", rejected_provider="Anthropic")
        assert decision.advance_model is False
        assert set(decision.excluded_providers) == {"Azure", "Anthropic"}


class TestBudgetCap:
    def test_budget_exhausted_advances_model(self) -> None:
        tracker = ProviderRotationTracker(max_rotations_per_model=2)
        tracker.on_provider_rejection(model="m", rejected_provider="Azure")
        tracker.on_provider_rejection(model="m", rejected_provider="Anthropic")
        # Third would exceed budget — caller must advance.
        decision = tracker.on_provider_rejection(model="m", rejected_provider="Together")
        assert decision.advance_model is True

    def test_zero_budget_advances_immediately(self) -> None:
        tracker = ProviderRotationTracker(max_rotations_per_model=0)
        decision = tracker.on_provider_rejection(model="m", rejected_provider="Azure")
        assert decision.advance_model is True

    def test_negative_budget_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProviderRotationTracker(max_rotations_per_model=-1)


class TestPerModelIsolation:
    def test_excluded_set_is_per_model(self) -> None:
        tracker = ProviderRotationTracker(max_rotations_per_model=2)
        tracker.on_provider_rejection(model="model-a", rejected_provider="Azure")
        tracker.on_provider_rejection(model="model-a", rejected_provider="Anthropic")
        decision = tracker.on_provider_rejection(model="model-b", rejected_provider="Azure")
        # Fresh model — budget not exhausted, excluded list starts over.
        assert decision.advance_model is False
        assert decision.excluded_providers == ("Azure",)

    def test_excluded_providers_for_unknown_model(self) -> None:
        tracker = ProviderRotationTracker(max_rotations_per_model=2)
        assert tracker.excluded_providers_for("never-touched") == ()


class TestObservability:
    def test_each_rotation_emits_event(self) -> None:
        events: list[dict] = []
        tracker = ProviderRotationTracker(
            max_rotations_per_model=2,
            audit=lambda payload: events.append(payload),
            correlation_id="req-7",
        )
        tracker.on_provider_rejection(model="m", rejected_provider="Azure")
        assert len(events) == 1
        evt = events[0]
        assert evt["event"] == "openrouter_provider_rotation"
        assert evt["model"] == "m"
        assert evt["excluded_provider"] == "Azure"
        assert evt["correlation_id"] == "req-7"

    def test_budget_exhausted_emits_distinct_event(self) -> None:
        events: list[dict] = []
        tracker = ProviderRotationTracker(
            max_rotations_per_model=1,
            audit=lambda payload: events.append(payload),
        )
        tracker.on_provider_rejection(model="m", rejected_provider="Azure")
        tracker.on_provider_rejection(model="m", rejected_provider="Anthropic")
        # Second one exhausted the budget — distinct event type.
        kinds = [e["event"] for e in events]
        assert "openrouter_provider_rotation_exhausted" in kinds


