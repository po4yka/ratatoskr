"""Prometheus metrics for GitHub repository sync and search.

Counters and histograms for:
- github_stars_sync task runs and per-run repo counts
- Repository semantic search latency

Usage:
    from app.observability.metrics_repositories import (
        GITHUB_SYNC_RUNS_TOTAL,
        REPOSITORY_SEARCH_LATENCY_SECONDS,
    )

    GITHUB_SYNC_RUNS_TOTAL.labels(status="ok").inc()

    with REPOSITORY_SEARCH_LATENCY_SECONDS.time():
        results = await search(...)
"""

from __future__ import annotations

from app.observability.metrics import PROMETHEUS_AVAILABLE, REGISTRY

if PROMETHEUS_AVAILABLE:
    from prometheus_client import Counter, Gauge, Histogram

    GITHUB_SYNC_RUNS_TOTAL = Counter(
        "ratatoskr_github_sync_runs_total",
        "Number of github_stars_sync runs by status",
        ["status"],  # "ok" | "partial" | "failed" | "ratelimited"
        registry=REGISTRY,
    )

    GITHUB_SYNC_RATE_LIMITED_TOTAL = Counter(
        "ratatoskr_github_sync_rate_limited_total",
        "GitHub sync runs that hit a GitHub API rate limit by user",
        ["user_id"],
        registry=REGISTRY,
    )

    GITHUB_SYNC_RATE_LIMIT_STREAK = Gauge(
        "ratatoskr_github_sync_rate_limit_streak",
        "Consecutive GitHub sync runs rate-limited for a user",
        ["user_id"],
        multiprocess_mode="mostrecent",
        registry=REGISTRY,
    )

    GITHUB_SYNC_REPOS_IMPORTED_TOTAL = Counter(
        "ratatoskr_github_sync_repos_imported_total",
        "Total newly imported starred repositories across all sync runs",
        registry=REGISTRY,
    )

    GITHUB_SYNC_REPOS_UPDATED_TOTAL = Counter(
        "ratatoskr_github_sync_repos_updated_total",
        "Total starred repositories updated (metadata refresh) across all sync runs",
        registry=REGISTRY,
    )

    GITHUB_SYNC_REPOS_UNSTARRED_TOTAL = Counter(
        "ratatoskr_github_sync_repos_unstarred_total",
        "Total repositories soft-unstarred across all sync runs",
        registry=REGISTRY,
    )

    GITHUB_SYNC_LLM_CALLS_TOTAL = Counter(
        "ratatoskr_github_sync_llm_calls_total",
        "Repository analysis LLM calls during sync, by trigger",
        ["trigger"],  # "made" | "deferred"
        registry=REGISTRY,
    )

    GITHUB_API_RATE_LIMIT_HITS_TOTAL = Counter(
        "ratatoskr_github_api_rate_limit_hits_total",
        "GitHub REST API responses classified as a rate limit (429 or 403 limit)",
        registry=REGISTRY,
    )

    GITHUB_PENDING_ANALYSIS_BACKLOG = Gauge(
        "ratatoskr_github_pending_analysis_backlog",
        "Repositories awaiting analysis (pending_analysis=True) after the last sync run",
        multiprocess_mode="mostrecent",
        registry=REGISTRY,
    )

    GITHUB_REPOSITORY_WATCH_TRIGGERS_TOTAL = Counter(
        "ratatoskr_github_repository_watch_triggers_total",
        "Repository watch events emitted by trigger type",
        ["trigger"],  # "readme" | "release"
        registry=REGISTRY,
    )

    REPOSITORY_SEARCH_LATENCY_SECONDS = Histogram(
        "ratatoskr_repository_search_latency_seconds",
        "Repository semantic search latency",
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        registry=REGISTRY,
    )

else:
    GITHUB_SYNC_RUNS_TOTAL = None
    GITHUB_SYNC_RATE_LIMITED_TOTAL = None
    GITHUB_SYNC_RATE_LIMIT_STREAK = None
    GITHUB_SYNC_REPOS_IMPORTED_TOTAL = None
    GITHUB_SYNC_REPOS_UPDATED_TOTAL = None
    GITHUB_SYNC_REPOS_UNSTARRED_TOTAL = None
    GITHUB_SYNC_LLM_CALLS_TOTAL = None
    GITHUB_API_RATE_LIMIT_HITS_TOTAL = None
    GITHUB_PENDING_ANALYSIS_BACKLOG = None
    GITHUB_REPOSITORY_WATCH_TRIGGERS_TOTAL = None
    REPOSITORY_SEARCH_LATENCY_SECONDS = None
