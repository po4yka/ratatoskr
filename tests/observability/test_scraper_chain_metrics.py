"""Tests for scraper-chain failure / latency telemetry.

Two new Prometheus signals:
  * scraper_attempts_total{provider,status}      - counter
  * scraper_attempt_latency_seconds{provider}    - histogram
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def metrics_module() -> object:
    return importlib.import_module("app.observability.metrics")


class TestScraperAttemptsCounter:
    def test_record_attempt_success_increments(self, metrics_module) -> None:
        if not metrics_module.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")
        metric = metrics_module.SCRAPER_ATTEMPTS_TOTAL
        before = metric.labels(provider="scrapling", status="success")._value.get()
        metrics_module.record_scraper_attempt(provider="scrapling", status="success")
        assert metric.labels(provider="scrapling", status="success")._value.get() == (before + 1.0)

    def test_record_attempt_error_status(self, metrics_module) -> None:
        if not metrics_module.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")
        metric = metrics_module.SCRAPER_ATTEMPTS_TOTAL
        before = metric.labels(provider="firecrawl", status="timeout")._value.get()
        metrics_module.record_scraper_attempt(provider="firecrawl", status="timeout")
        assert metric.labels(provider="firecrawl", status="timeout")._value.get() == (before + 1.0)

    def test_record_attempt_noop_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, metrics_module
    ) -> None:
        monkeypatch.setattr(metrics_module, "PROMETHEUS_AVAILABLE", False)
        metrics_module.record_scraper_attempt(provider="x", status="error")


class TestScraperLatencyHistogram:
    def test_record_latency_observes(self, metrics_module) -> None:
        if not metrics_module.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")
        metric = metrics_module.SCRAPER_ATTEMPT_LATENCY_SECONDS
        before = metric.labels(provider="playwright")._sum.get()
        metrics_module.record_scraper_attempt_latency(provider="playwright", latency_seconds=3.7)
        assert metric.labels(provider="playwright")._sum.get() == pytest.approx(before + 3.7)

    def test_record_latency_drops_negative(self, metrics_module) -> None:
        metrics_module.record_scraper_attempt_latency(provider="playwright", latency_seconds=-0.1)


class TestExposedInMetricsEndpoint:
    def test_metrics_endpoint_includes_signals(self, metrics_module) -> None:
        if not metrics_module.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")
        metrics_module.record_scraper_attempt(provider="probe", status="success")
        metrics_module.record_scraper_attempt_latency(provider="probe", latency_seconds=1.2)
        payload = metrics_module.get_metrics().decode("utf-8")
        assert "ratatoskr_scraper_attempts_total" in payload
        assert "ratatoskr_scraper_attempt_latency_seconds" in payload


class TestAttemptLogSerialization:
    """The attempt_log payload is a list of dicts; each entry records
    a single provider call so multi-provider failure paths are
    auditable in the crawl_results row."""

    def test_serialize_attempt_log_produces_list_of_dicts(self) -> None:
        from app.adapters.content.scraper.attempt_log import (
            ScraperAttemptEntry,
            serialize_attempt_log,
        )

        entries = [
            ScraperAttemptEntry(
                provider="scrapling",
                status="error",
                latency_ms=1234,
                error_class="ScrapingTimeout",
            ),
            ScraperAttemptEntry(
                provider="firecrawl",
                status="success",
                latency_ms=987,
                error_class=None,
            ),
        ]
        out = serialize_attempt_log(entries)
        assert isinstance(out, list)
        assert out == [
            {
                "provider": "scrapling",
                "status": "error",
                "latency_ms": 1234,
                "error_class": "ScrapingTimeout",
                "error_message": None,
                "bytes_extracted": None,
            },
            {
                "provider": "firecrawl",
                "status": "success",
                "latency_ms": 987,
                "error_class": None,
                "error_message": None,
                "bytes_extracted": None,
            },
        ]

    def test_empty_attempt_log_serializes_to_empty_list(self) -> None:
        from app.adapters.content.scraper.attempt_log import serialize_attempt_log

        assert serialize_attempt_log([]) == []

    def test_attempt_entry_status_must_be_known(self) -> None:
        from app.adapters.content.scraper.attempt_log import ScraperAttemptEntry

        # Allowed statuses per task spec: success | error | timeout | skipped.
        for status in ("success", "error", "timeout", "skipped"):
            ScraperAttemptEntry(provider="x", status=status, latency_ms=0, error_class=None)
        with pytest.raises(ValueError):
            ScraperAttemptEntry(provider="x", status="wibble", latency_ms=0, error_class=None)

    def test_partial_failure_path_collects_multiple_entries(self) -> None:
        from app.adapters.content.scraper.attempt_log import (
            ScraperAttemptEntry,
            ScraperAttemptRecorder,
        )

        recorder = ScraperAttemptRecorder()
        recorder.record(
            ScraperAttemptEntry(
                provider="scrapling", status="error", latency_ms=10, error_class="X"
            )
        )
        recorder.record(
            ScraperAttemptEntry(
                provider="firecrawl", status="timeout", latency_ms=30000, error_class="T"
            )
        )
        recorder.record(
            ScraperAttemptEntry(
                provider="playwright", status="success", latency_ms=2500, error_class=None
            )
        )
        assert len(recorder.entries) == 3
        assert recorder.winner() == "playwright"
        # Failed providers list excludes the winner.
        assert set(recorder.failed_providers()) == {"scrapling", "firecrawl"}

    def test_no_success_returns_no_winner(self) -> None:
        from app.adapters.content.scraper.attempt_log import (
            ScraperAttemptEntry,
            ScraperAttemptRecorder,
        )

        recorder = ScraperAttemptRecorder()
        recorder.record(
            ScraperAttemptEntry(
                provider="scrapling", status="error", latency_ms=10, error_class="X"
            )
        )
        assert recorder.winner() is None
        assert recorder.failed_providers() == ["scrapling"]
