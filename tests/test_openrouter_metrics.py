"""Unit tests for per-model OpenRouter telemetry helpers (Improvement B).

Verifies that:
- record_per_model_timeout increments the correct counter label.
- record_per_model_latency observes into the correct histogram bucket.
- record_per_model_circuit_breaker_state sets a single integer gauge per model
  (0=closed, 1=half_open, 2=open) — the old 3-series label-per-state pattern
  has been removed.
- _bucket_model() collapses unknown model IDs to "other".
- _bucket_platform() collapses unknown platform values to "other".

Tests are skipped when prometheus_client is not installed.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.observability.metrics as _metrics_mod

pytestmark = pytest.mark.skipif(
    not _metrics_mod.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed",
)


def _counter_value(counter: Any, **labels: Any) -> float:
    return counter.labels(**labels)._value.get()


def _gauge_value(gauge: Any, **labels: Any) -> float:
    return gauge.labels(**labels)._value.get()


def _histogram_count(histogram: Any, **labels: Any) -> float:
    labelled = histogram.labels(**labels)
    for metric in labelled.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return float(sample.value)
    raise AssertionError("histogram count sample not found")


def test_record_per_model_timeout_increments_counter() -> None:
    from app.observability.metrics import (
        OPENROUTER_PER_MODEL_TIMEOUT,
        _DEFAULT_MODEL_ALLOWLIST,
        record_per_model_timeout,
    )

    # Use an allowlisted model so the label is not bucketed to "other".
    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    before = _counter_value(OPENROUTER_PER_MODEL_TIMEOUT, model=model)
    record_per_model_timeout(model=model)
    after = _counter_value(OPENROUTER_PER_MODEL_TIMEOUT, model=model)
    assert after == before + 1.0


def test_record_per_model_timeout_unknown_model_buckets_to_other() -> None:
    """An unknown model ID must be reported under the 'other' label."""
    from app.observability.metrics import OPENROUTER_PER_MODEL_TIMEOUT, record_per_model_timeout

    before = _counter_value(OPENROUTER_PER_MODEL_TIMEOUT, model="other")
    record_per_model_timeout(model="unknown/brand-new-model-xyz")
    after = _counter_value(OPENROUTER_PER_MODEL_TIMEOUT, model="other")
    assert after == before + 1.0


def test_record_per_model_timeout_is_noop_without_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.observability.metrics as m

    monkeypatch.setattr(m, "PROMETHEUS_AVAILABLE", False)
    # Should not raise.
    m.record_per_model_timeout(model="test/noprometheus")


def test_record_per_model_latency_observes_histogram() -> None:
    from app.observability.metrics import (
        OPENROUTER_PER_MODEL_LATENCY,
        _DEFAULT_MODEL_ALLOWLIST,
        record_per_model_latency,
    )

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    before_count = _histogram_count(OPENROUTER_PER_MODEL_LATENCY, model=model, outcome="success")
    record_per_model_latency(model=model, outcome="success", seconds=1.5)
    after_count = _histogram_count(OPENROUTER_PER_MODEL_LATENCY, model=model, outcome="success")
    assert after_count == before_count + 1


def test_record_per_model_latency_is_noop_without_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.observability.metrics as m

    monkeypatch.setattr(m, "PROMETHEUS_AVAILABLE", False)
    m.record_per_model_latency(model="test/x", outcome="timeout", seconds=99.0)


# ---------------------------------------------------------------------------
# Circuit-breaker: single integer gauge per bucketed model
# ---------------------------------------------------------------------------


def test_record_per_model_circuit_breaker_state_open_writes_2() -> None:
    """open state must write integer 2 to the single-label gauge."""
    from app.observability.metrics import (
        OPENROUTER_CIRCUIT_BREAKER_STATE,
        _DEFAULT_MODEL_ALLOWLIST,
        record_per_model_circuit_breaker_state,
    )

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    record_per_model_circuit_breaker_state(model=model, state="open")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model=model) == 2.0


def test_record_per_model_circuit_breaker_state_half_open_writes_1() -> None:
    """half_open state must write integer 1."""
    from app.observability.metrics import (
        OPENROUTER_CIRCUIT_BREAKER_STATE,
        _DEFAULT_MODEL_ALLOWLIST,
        record_per_model_circuit_breaker_state,
    )

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    record_per_model_circuit_breaker_state(model=model, state="half_open")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model=model) == 1.0


def test_record_per_model_circuit_breaker_state_closed_writes_0() -> None:
    """closed state must write integer 0."""
    from app.observability.metrics import (
        OPENROUTER_CIRCUIT_BREAKER_STATE,
        _DEFAULT_MODEL_ALLOWLIST,
        record_per_model_circuit_breaker_state,
    )

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    record_per_model_circuit_breaker_state(model=model, state="closed")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model=model) == 0.0


def test_record_per_model_circuit_breaker_state_transitions() -> None:
    """Gauge value must track state transitions: open->half_open->closed."""
    from app.observability.metrics import (
        OPENROUTER_CIRCUIT_BREAKER_STATE,
        _DEFAULT_MODEL_ALLOWLIST,
        record_per_model_circuit_breaker_state,
    )

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    record_per_model_circuit_breaker_state(model=model, state="open")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model=model) == 2.0

    record_per_model_circuit_breaker_state(model=model, state="half_open")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model=model) == 1.0

    record_per_model_circuit_breaker_state(model=model, state="closed")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model=model) == 0.0


def test_record_per_model_circuit_breaker_unknown_model_buckets_to_other() -> None:
    """Unknown model must be stored under the 'other' label."""
    from app.observability.metrics import (
        OPENROUTER_CIRCUIT_BREAKER_STATE,
        record_per_model_circuit_breaker_state,
    )

    record_per_model_circuit_breaker_state(model="never/seen/before", state="open")
    assert _gauge_value(OPENROUTER_CIRCUIT_BREAKER_STATE, model="other") == 2.0


def test_record_per_model_circuit_breaker_state_is_noop_without_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.observability.metrics as m

    monkeypatch.setattr(m, "PROMETHEUS_AVAILABLE", False)
    m.record_per_model_circuit_breaker_state(model="test/y", state="open")


# ---------------------------------------------------------------------------
# _bucket_model: unknown model -> "other"
# ---------------------------------------------------------------------------


def test_bucket_model_known_model_passes_through() -> None:
    from app.observability.metrics import _DEFAULT_MODEL_ALLOWLIST, _bucket_model

    model = next(iter(_DEFAULT_MODEL_ALLOWLIST))
    assert _bucket_model(model) == model


def test_bucket_model_unknown_model_returns_other() -> None:
    from app.observability.metrics import _bucket_model

    assert _bucket_model("totally/unknown-model-2099") == "other"


def test_bucket_model_other_string_passes_through() -> None:
    from app.observability.metrics import _bucket_model

    # "other" itself must never be double-bucketed.
    assert _bucket_model("other") == "other"


def test_configure_model_allowlist_adds_custom_model() -> None:
    """configure_model_allowlist() makes a custom model pass through."""
    import app.observability.metrics as m

    original = m._model_allowlist
    try:
        m.configure_model_allowlist({"my/custom-model-v1"})
        assert m._bucket_model("my/custom-model-v1") == "my/custom-model-v1"
        assert m._bucket_model("some/other-model") == "other"
    finally:
        # Restore original allowlist so other tests are not affected.
        m._model_allowlist = original


# ---------------------------------------------------------------------------
# _bucket_platform: unknown platform -> "other"
# ---------------------------------------------------------------------------


def test_bucket_platform_known_platforms_pass_through() -> None:
    from app.observability.metrics import _KNOWN_PLATFORMS, _bucket_platform

    for platform in _KNOWN_PLATFORMS:
        assert _bucket_platform(platform) == platform


def test_bucket_platform_unknown_platform_returns_other() -> None:
    from app.observability.metrics import _bucket_platform

    assert _bucket_platform("tiktok") == "other"
    assert _bucket_platform("mastodon") == "other"
    assert _bucket_platform("") == "other"
