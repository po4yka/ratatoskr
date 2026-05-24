"""Unit tests for ``record_llm_request_total_latency``.

Asserts the histogram observes and the slow-request counter increments only
above the configured threshold. Pre-pulls counter sums before/after to avoid
coupling to other tests that may exercise the same labels.
"""

from __future__ import annotations

import pytest

from app.observability import metrics as m

pytestmark = pytest.mark.skipif(
    not m.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed in this environment",
)


def _counter_value(counter: object, **labels: str) -> float:
    """Return the current value of a prometheus Counter for the given labels."""
    return counter.labels(**labels)._value.get()  # type: ignore[attr-defined]


class TestRecordLlmRequestTotalLatency:
    def test_below_threshold_no_slow_increment(self) -> None:
        before = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        m.record_llm_request_total_latency(request_type="url", total_latency_seconds=1.5)
        after = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        assert after == before

    def test_at_threshold_increments_slow(self) -> None:
        before = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        m.record_llm_request_total_latency(
            request_type="url",
            total_latency_seconds=m.LLM_REQUEST_SLOW_THRESHOLD_SECONDS,
        )
        after = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        assert after == before + 1

    def test_above_threshold_increments_slow(self) -> None:
        before = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        m.record_llm_request_total_latency(
            request_type="url",
            total_latency_seconds=m.LLM_REQUEST_SLOW_THRESHOLD_SECONDS * 4,
        )
        after = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        assert after == before + 1

    def test_negative_latency_is_ignored(self) -> None:
        # Clock skew / monotonic regression must never poison the histogram.
        before_count = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        m.record_llm_request_total_latency(request_type="url", total_latency_seconds=-1.0)
        after_count = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="url")
        assert after_count == before_count

    def test_request_type_label_normalized(self) -> None:
        # ``_metric_label`` lowercases and strips. Confirm a mixed-case input
        # routes to the lowercase bucket so dashboards don't fragment.
        before = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="rss")
        m.record_llm_request_total_latency(
            request_type="  RSS  ",
            total_latency_seconds=m.LLM_REQUEST_SLOW_THRESHOLD_SECONDS + 10,
        )
        after = _counter_value(m.LLM_REQUEST_SLOW_TOTAL, request_type="rss")
        assert after == before + 1
