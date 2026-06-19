"""Tests for app.observability.metrics_repositories."""

from __future__ import annotations

import pytest

from app.observability import metrics as _metrics_mod, metrics_repositories as repo_metrics


@pytest.mark.skipif(not _metrics_mod.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_metrics_module_exports() -> None:
    """All six metrics are importable and have expected names/types."""
    from prometheus_client import Counter, Histogram

    assert isinstance(repo_metrics.GITHUB_SYNC_RUNS_TOTAL, Counter)
    assert repo_metrics.GITHUB_SYNC_RUNS_TOTAL._name == "ratatoskr_github_sync_runs"

    assert isinstance(repo_metrics.GITHUB_SYNC_RATE_LIMITED_TOTAL, Counter)
    assert repo_metrics.GITHUB_SYNC_RATE_LIMITED_TOTAL._name == "ratatoskr_github_sync_rate_limited"

    from prometheus_client import Gauge

    assert isinstance(repo_metrics.GITHUB_SYNC_RATE_LIMIT_STREAK, Gauge)
    assert (
        repo_metrics.GITHUB_SYNC_RATE_LIMIT_STREAK._name
        == "ratatoskr_github_sync_rate_limit_streak"
    )

    assert isinstance(repo_metrics.GITHUB_SYNC_REPOS_IMPORTED_TOTAL, Counter)
    assert (
        repo_metrics.GITHUB_SYNC_REPOS_IMPORTED_TOTAL._name
        == "ratatoskr_github_sync_repos_imported"
    )

    assert isinstance(repo_metrics.GITHUB_SYNC_REPOS_UPDATED_TOTAL, Counter)
    assert (
        repo_metrics.GITHUB_SYNC_REPOS_UPDATED_TOTAL._name == "ratatoskr_github_sync_repos_updated"
    )

    assert isinstance(repo_metrics.GITHUB_SYNC_REPOS_UNSTARRED_TOTAL, Counter)
    assert (
        repo_metrics.GITHUB_SYNC_REPOS_UNSTARRED_TOTAL._name
        == "ratatoskr_github_sync_repos_unstarred"
    )

    assert isinstance(repo_metrics.GITHUB_SYNC_LLM_CALLS_TOTAL, Counter)
    assert repo_metrics.GITHUB_SYNC_LLM_CALLS_TOTAL._name == "ratatoskr_github_sync_llm_calls"

    assert isinstance(repo_metrics.REPOSITORY_SEARCH_LATENCY_SECONDS, Histogram)
    assert (
        repo_metrics.REPOSITORY_SEARCH_LATENCY_SECONDS._name
        == "ratatoskr_repository_search_latency_seconds"
    )


@pytest.mark.skipif(not _metrics_mod.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_sync_run_increments_status_counter() -> None:
    """Direct counter increment for status='ok' is reflected in the registry."""
    counter = repo_metrics.GITHUB_SYNC_RUNS_TOTAL
    assert counter is not None

    registry = _metrics_mod.REGISTRY
    assert registry is not None

    before = registry.get_sample_value("ratatoskr_github_sync_runs_total", {"status": "ok"}) or 0.0

    counter.labels(status="ok").inc()

    after = registry.get_sample_value("ratatoskr_github_sync_runs_total", {"status": "ok"}) or 0.0

    assert after - before == pytest.approx(1.0)


@pytest.mark.skipif(not _metrics_mod.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_search_latency_histogram_observes() -> None:
    """Histogram records at least one sample after a timed block."""
    histogram = repo_metrics.REPOSITORY_SEARCH_LATENCY_SECONDS
    assert histogram is not None

    registry = _metrics_mod.REGISTRY
    assert registry is not None

    before = registry.get_sample_value("ratatoskr_repository_search_latency_seconds_count") or 0.0

    with histogram.time():
        pass  # minimal timed block

    after = registry.get_sample_value("ratatoskr_repository_search_latency_seconds_count") or 0.0

    assert after - before >= 1.0


@pytest.mark.skipif(not _metrics_mod.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_github_api_rate_limit_and_pending_backlog_metrics() -> None:
    """Rate-limit Counter increments and the pending-backlog Gauge sets in the registry."""
    from prometheus_client import Counter, Gauge

    rate_limit = repo_metrics.GITHUB_API_RATE_LIMIT_HITS_TOTAL
    backlog = repo_metrics.GITHUB_PENDING_ANALYSIS_BACKLOG
    assert isinstance(rate_limit, Counter)
    assert rate_limit._name == "ratatoskr_github_api_rate_limit_hits"
    assert isinstance(backlog, Gauge)
    assert backlog._name == "ratatoskr_github_pending_analysis_backlog"

    registry = _metrics_mod.REGISTRY
    assert registry is not None

    before = registry.get_sample_value("ratatoskr_github_api_rate_limit_hits_total") or 0.0
    rate_limit.inc()
    after = registry.get_sample_value("ratatoskr_github_api_rate_limit_hits_total") or 0.0
    assert after - before == pytest.approx(1.0)

    backlog.set(7)
    assert registry.get_sample_value("ratatoskr_github_pending_analysis_backlog") == pytest.approx(
        7.0
    )
