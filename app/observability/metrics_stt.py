"""Prometheus metrics for speech-to-text requests."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

_STT_OUTCOMES = frozenset({"success", "error", "disabled", "duration_exceeded", "empty"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter

    STT_REQUESTS_TOTAL = Counter(
        "ratatoskr_stt_requests_total",
        "Speech-to-text request outcomes",
        ["outcome"],
        registry=REGISTRY,
    )
    STT_AUDIO_SECONDS_TOTAL = Counter(
        "ratatoskr_stt_audio_seconds_total",
        "Total audio seconds accepted by speech-to-text",
        registry=REGISTRY,
    )
else:
    STT_REQUESTS_TOTAL = None
    STT_AUDIO_SECONDS_TOTAL = None


def record_stt_request(outcome: str) -> None:
    """Record one speech-to-text request outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    label = _metric_label(outcome)
    STT_REQUESTS_TOTAL.labels(outcome=label if label in _STT_OUTCOMES else "error").inc()


def record_stt_audio_seconds(duration_seconds: float | None) -> None:
    """Add accepted audio duration to the cost-tracking counter."""
    if not PROMETHEUS_AVAILABLE or duration_seconds is None:
        return
    STT_AUDIO_SECONDS_TOTAL.inc(max(0.0, float(duration_seconds)))


__all__ = [
    "STT_AUDIO_SECONDS_TOTAL",
    "STT_REQUESTS_TOTAL",
    "record_stt_audio_seconds",
    "record_stt_request",
]
