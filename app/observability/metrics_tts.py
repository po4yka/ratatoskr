"""Prometheus metrics for text-to-speech requests."""

from __future__ import annotations

from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY, _metric_label

_TTS_OUTCOMES = frozenset({"success", "retry", "quota_exceeded", "http_error", "timeout"})

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Histogram

    TTS_REQUESTS_TOTAL = Counter(
        "ratatoskr_tts_requests_total",
        "Text-to-speech request outcomes",
        ["outcome"],
        registry=REGISTRY,
    )
    TTS_AUDIO_BYTES_TOTAL = Counter(
        "ratatoskr_tts_audio_bytes_total",
        "Total text-to-speech audio bytes synthesized",
        registry=REGISTRY,
    )
    TTS_LATENCY_SECONDS = Histogram(
        "ratatoskr_tts_latency_seconds",
        "Text-to-speech request latency in seconds",
        registry=REGISTRY,
    )
else:
    TTS_REQUESTS_TOTAL = None
    TTS_AUDIO_BYTES_TOTAL = None
    TTS_LATENCY_SECONDS = None


def record_tts_request(outcome: str) -> None:
    """Record one text-to-speech request outcome."""
    if not PROMETHEUS_AVAILABLE:
        return
    label = _metric_label(outcome)
    TTS_REQUESTS_TOTAL.labels(outcome=label if label in _TTS_OUTCOMES else "http_error").inc()


def record_tts_audio_bytes(byte_count: int | None) -> None:
    """Add synthesized audio bytes to the cost-tracking counter."""
    if not PROMETHEUS_AVAILABLE or byte_count is None:
        return
    TTS_AUDIO_BYTES_TOTAL.inc(max(0, int(byte_count)))


def record_tts_latency(duration_seconds: float | None) -> None:
    """Observe one text-to-speech request latency."""
    if not PROMETHEUS_AVAILABLE or duration_seconds is None:
        return
    TTS_LATENCY_SECONDS.observe(max(0.0, float(duration_seconds)))


__all__ = [
    "TTS_AUDIO_BYTES_TOTAL",
    "TTS_LATENCY_SECONDS",
    "TTS_REQUESTS_TOTAL",
    "record_tts_audio_bytes",
    "record_tts_latency",
    "record_tts_request",
]
