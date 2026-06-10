"""Unit tests for scraper chain Prometheus metric instrumentation.

Tests verify that the correct counters are incremented (and not incremented)
on each outcome path in ContentScraperChain._attempt_provider, covering the
four chain-level metric families:

    ratatoskr_scraper_chain_attempts_total
    ratatoskr_scraper_chain_successes_total
    ratatoskr_scraper_chain_failures_total
    ratatoskr_scraper_chain_duration_seconds

The tests also cover the cross-cutting total-latency histogram:

    ratatoskr_scraper_chain_total_latency_seconds

Strategy: use actual prometheus_client Counter/Histogram objects (not mocked)
by reading the current label value before the action and asserting the delta.
This avoids import-order coupling and stays robust to metric-object
initialization order.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.observability import metrics as metrics_module


# ---------------------------------------------------------------------------
# Low-level prometheus_client introspection helpers
# ---------------------------------------------------------------------------


def _counter_value(counter: Any, **labels: str) -> float:
    """Return the current value of a Counter label combination.

    Returns 0.0 when prometheus_client is unavailable or the label set has
    not been observed yet (prometheus_client creates the label child lazily).
    """
    if counter is None:
        return 0.0
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


def _histogram_sum(histogram: Any, **labels: str) -> float:
    """Return the running sum of a Histogram label combination.

    A non-zero sum is sufficient evidence that at least one observation was
    recorded.  We use _sum rather than _count because _count resets on some
    prometheus_client builds while _sum is always monotone.
    """
    if histogram is None:
        return 0.0
    try:
        return histogram.labels(**labels)._sum.get()
    except Exception:
        return 0.0



# ---------------------------------------------------------------------------
# Test-fixture builders
# ---------------------------------------------------------------------------


def _ok_result(content: str = "Hello world " * 50) -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.OK,
        content_markdown=content,
        source_url="https://example.com/article",
        endpoint="test",
    )


def _empty_result() -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.ERROR,
        content_markdown="",
        content_html="",
        error_text="no content",
        source_url="https://example.com/article",
        endpoint="test",
    )


def _error_page_result() -> FirecrawlResult:
    """Short content that matches the error-page regex inside the chain."""
    return FirecrawlResult(
        status=CallStatus.OK,
        content_markdown="404 not found",
        source_url="https://example.com/article",
        endpoint="test",
    )


def _make_provider(name: str, result: FirecrawlResult) -> MagicMock:
    provider = MagicMock()
    provider.provider_name = name
    provider.scrape_markdown = AsyncMock(return_value=result)
    return provider


def _make_raising_provider(name: str, exc: Exception) -> MagicMock:
    provider = MagicMock()
    provider.provider_name = name
    provider.scrape_markdown = AsyncMock(side_effect=exc)
    return provider


def _make_chain(*providers: Any, min_content_length: int = 0) -> Any:
    from app.adapters.content.scraper.chain import ContentScraperChain

    return ContentScraperChain(
        list(providers),
        min_content_length=min_content_length,
        race_enabled=False,
    )


# Patch target used in every test to avoid real DNS/SSRF checks.
_SAFE_URL = patch(
    "app.adapters.content.scraper.chain.is_url_safe_async",
    return_value=(True, ""),
)


# ---------------------------------------------------------------------------
# 1. Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_chain_attempts_increments() -> None:
    """chain_attempts_total increments once for the winning provider."""
    name = "scrapling"
    provider = _make_provider(name, _ok_result())
    chain = _make_chain(provider)

    before = _counter_value(metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=name)

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=name)
    assert after - before == pytest.approx(1.0), "chain_attempts_total must increment by 1 on success"


@pytest.mark.asyncio
async def test_success_chain_successes_increments() -> None:
    """chain_successes_total increments once when content passes quality gates."""
    name = "scrapling"
    provider = _make_provider(name, _ok_result())
    chain = _make_chain(provider)

    before = _counter_value(metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=name)

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=name)
    assert after - before == pytest.approx(1.0), "chain_successes_total must increment by 1 on success"


@pytest.mark.asyncio
async def test_success_chain_failures_not_incremented() -> None:
    """chain_failures_total must NOT increment when provider succeeds."""
    name = "scrapling"
    provider = _make_provider(name, _ok_result())
    chain = _make_chain(provider)

    before_empty = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="empty"
    )
    before_error = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="error"
    )
    before_too_short = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="too_short"
    )

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="empty"
    ) == before_empty, "failures{empty} must not increment on success"
    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="error"
    ) == before_error, "failures{error} must not increment on success"
    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="too_short"
    ) == before_too_short, "failures{too_short} must not increment on success"


@pytest.mark.asyncio
async def test_success_chain_duration_observed() -> None:
    """record_scraper_chain_duration is called exactly once for the winning provider.

    In fast unit tests time.monotonic() differences collapse to 0.0 so the
    Histogram _sum stays at 0.0; we therefore assert on the call itself rather
    than on the _sum delta.
    """
    name = "defuddle"
    provider = _make_provider(name, _ok_result())
    chain = _make_chain(provider)

    with _SAFE_URL, patch(
        "app.adapters.content.scraper.chain.record_scraper_chain_duration"
    ) as mock_dur:
        await chain.scrape_markdown("https://example.com/article")

    mock_dur.assert_called_once_with(provider=name, latency_seconds=pytest.approx(0.0, abs=5.0))


@pytest.mark.asyncio
async def test_success_chain_total_latency_observed() -> None:
    """chain_total_latency_seconds must be observed with outcome=success."""
    name = "firecrawl"
    provider = _make_provider(name, _ok_result())
    chain = _make_chain(provider)

    before_sum = _histogram_sum(
        metrics_module.SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS,
        mode="serial",
        outcome="success",
    )

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after_sum = _histogram_sum(
        metrics_module.SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS,
        mode="serial",
        outcome="success",
    )
    assert after_sum > before_sum, "chain_total_latency_seconds{success} must be observed"


# ---------------------------------------------------------------------------
# 2. Failure path — empty result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_result_chain_failures_empty_increments() -> None:
    """chain_failures_total{reason=empty} increments when provider returns no content."""
    name = "crawl4ai"
    provider = _make_provider(name, _empty_result())
    chain = _make_chain(provider)

    before = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="empty"
    )

    with _SAFE_URL:
        result = await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="empty"
    )
    assert after - before == pytest.approx(1.0), "failures{empty} must increment on empty result"
    assert result.status == CallStatus.ERROR


@pytest.mark.asyncio
async def test_empty_result_chain_successes_not_incremented() -> None:
    """chain_successes_total must NOT increment when provider returns empty content."""
    name = "crawl4ai"
    provider = _make_provider(name, _empty_result())
    chain = _make_chain(provider)

    before = _counter_value(metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=name)

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=name)
    assert after == before, "chain_successes_total must NOT increment on empty result"


@pytest.mark.asyncio
async def test_empty_result_chain_attempts_still_increments() -> None:
    """chain_attempts_total increments even when the provider produces no content."""
    name = "crawl4ai"
    provider = _make_provider(name, _empty_result())
    chain = _make_chain(provider)

    before = _counter_value(metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=name)

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=name)
    assert after - before == pytest.approx(1.0), "chain_attempts_total must increment even on failure"


# ---------------------------------------------------------------------------
# 3. Failure path — provider raises an exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_chain_failures_error_increments() -> None:
    """chain_failures_total{reason=error} increments when the provider raises."""
    name = "playwright"
    provider = _make_raising_provider(name, RuntimeError("connection refused"))
    chain = _make_chain(provider)

    before = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="error"
    )

    with _SAFE_URL:
        result = await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="error"
    )
    assert after - before == pytest.approx(1.0), "failures{error} must increment on exception"
    assert result.status == CallStatus.ERROR


@pytest.mark.asyncio
async def test_exception_chain_successes_not_incremented() -> None:
    """chain_successes_total must NOT increment when provider raises."""
    name = "playwright"
    provider = _make_raising_provider(name, RuntimeError("timeout"))
    chain = _make_chain(provider)

    before = _counter_value(metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=name)

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=name)
    assert after == before, "chain_successes_total must NOT increment on exception"


# ---------------------------------------------------------------------------
# 4. Failure path — error page detected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_page_chain_failures_error_page_increments() -> None:
    """chain_failures_total{reason=error_page} increments when content matches error-page pattern."""
    name = "direct_html"
    provider = _make_provider(name, _error_page_result())
    chain = _make_chain(provider)

    before = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="error_page"
    )

    with _SAFE_URL:
        result = await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="error_page"
    )
    assert after - before == pytest.approx(1.0), "failures{error_page} must increment on error page"
    assert result.status == CallStatus.ERROR


# ---------------------------------------------------------------------------
# 5. Failure path — content too short
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_short_chain_failures_too_short_increments() -> None:
    """chain_failures_total{reason=too_short} increments when content is below threshold."""
    name = "cloakbrowser"
    provider = _make_provider(name, _ok_result("Short."))
    chain = _make_chain(provider, min_content_length=500)

    before = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="too_short"
    )

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=name, reason="too_short"
    )
    assert after - before == pytest.approx(1.0), "failures{too_short} must increment on thin content"


# ---------------------------------------------------------------------------
# 6. Fallback: first provider fails, second succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_first_fails_second_succeeds_chain_metrics() -> None:
    """When the first provider fails and the second succeeds:
    - first:  chain_attempts+1, chain_failures{empty}+1, chain_successes unchanged
    - second: chain_attempts+1, chain_successes+1, chain_failures unchanged
    """
    failing_name = "defuddle"
    winning_name = "scrapling"

    failing_provider = _make_provider(failing_name, _empty_result())
    winning_provider = _make_provider(winning_name, _ok_result())
    chain = _make_chain(failing_provider, winning_provider)

    before_fail_attempts = _counter_value(
        metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=failing_name
    )
    before_fail_failures = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=failing_name, reason="empty"
    )
    before_fail_successes = _counter_value(
        metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=failing_name
    )
    before_win_attempts = _counter_value(
        metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=winning_name
    )
    before_win_successes = _counter_value(
        metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=winning_name
    )
    before_win_failures = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=winning_name, reason="empty"
    )

    with _SAFE_URL:
        result = await chain.scrape_markdown("https://example.com/article")

    assert result.status == CallStatus.OK

    # Failing provider assertions
    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=failing_name
    ) - before_fail_attempts == pytest.approx(1.0), "failing provider: chain_attempts+1"

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=failing_name, reason="empty"
    ) - before_fail_failures == pytest.approx(1.0), "failing provider: chain_failures{empty}+1"

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=failing_name
    ) == before_fail_successes, "failing provider: chain_successes must NOT increment"

    # Winning provider assertions
    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_ATTEMPTS_TOTAL, provider=winning_name
    ) - before_win_attempts == pytest.approx(1.0), "winning provider: chain_attempts+1"

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=winning_name
    ) - before_win_successes == pytest.approx(1.0), "winning provider: chain_successes+1"

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=winning_name, reason="empty"
    ) == before_win_failures, "winning provider: chain_failures must NOT increment"


@pytest.mark.asyncio
async def test_fallback_exception_then_success_chain_metrics() -> None:
    """When the first provider raises and the second succeeds:
    - first:  chain_failures{error}+1
    - second: chain_successes+1
    """
    failing_name = "crawlee"
    winning_name = "firecrawl"

    failing_provider = _make_raising_provider(failing_name, OSError("read timeout"))
    winning_provider = _make_provider(winning_name, _ok_result())
    chain = _make_chain(failing_provider, winning_provider)

    before_fail_failures = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=failing_name, reason="error"
    )
    before_win_successes = _counter_value(
        metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=winning_name
    )

    with _SAFE_URL:
        result = await chain.scrape_markdown("https://example.com/article")

    assert result.status == CallStatus.OK

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=failing_name, reason="error"
    ) - before_fail_failures == pytest.approx(1.0), "exception provider: chain_failures{error}+1"

    assert _counter_value(
        metrics_module.SCRAPER_CHAIN_SUCCESSES_TOTAL, provider=winning_name
    ) - before_win_successes == pytest.approx(1.0), "winning provider: chain_successes+1"


# ---------------------------------------------------------------------------
# 7. Duration histogram is recorded on every path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duration_histogram_recorded_on_failure() -> None:
    """record_scraper_chain_duration is called even when the provider returns empty content."""
    name = "scrapegraph_ai"
    provider = _make_provider(name, _empty_result())
    chain = _make_chain(provider)

    with _SAFE_URL, patch(
        "app.adapters.content.scraper.chain.record_scraper_chain_duration"
    ) as mock_dur:
        await chain.scrape_markdown("https://example.com/article")

    mock_dur.assert_called_once_with(provider=name, latency_seconds=pytest.approx(0.0, abs=5.0))


@pytest.mark.asyncio
async def test_duration_histogram_recorded_on_exception() -> None:
    """record_scraper_chain_duration is called even when the provider raises."""
    name = "webwright"
    provider = _make_raising_provider(name, RuntimeError("sidecar unavailable"))
    chain = _make_chain(provider)

    with _SAFE_URL, patch(
        "app.adapters.content.scraper.chain.record_scraper_chain_duration"
    ) as mock_dur:
        await chain.scrape_markdown("https://example.com/article")

    mock_dur.assert_called_once_with(provider=name, latency_seconds=pytest.approx(0.0, abs=5.0))


# ---------------------------------------------------------------------------
# 8. Total-latency histogram on non-success outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_latency_observed_when_all_providers_fail() -> None:
    """chain_total_latency_seconds{outcome=empty} must be observed when all providers fail."""
    name = "direct_pdf"
    provider = _make_provider(name, _empty_result())
    chain = _make_chain(provider)

    before_sum = _histogram_sum(
        metrics_module.SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS,
        mode="serial",
        outcome="empty",
    )

    with _SAFE_URL:
        await chain.scrape_markdown("https://example.com/article")

    after_sum = _histogram_sum(
        metrics_module.SCRAPER_CHAIN_TOTAL_LATENCY_SECONDS,
        mode="serial",
        outcome="empty",
    )
    assert after_sum > before_sum, "chain_total_latency_seconds{empty} must be observed on exhaustion"
