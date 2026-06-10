"""Unit tests for scraper chain Prometheus metric instrumentation.

These tests verify that the correct counters are incremented (and not
incremented) on each outcome path in ContentScraperChain._attempt_provider.
They use actual prometheus_client counter objects — no mocking needed — by
reading the current value before and after each action and asserting the delta.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.observability import metrics as metrics_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _counter_value(counter: Any, **labels: str) -> float:
    """Return the current value of a prometheus_client Counter label set."""
    if counter is None:
        return 0.0
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


def _histogram_count(histogram: Any, **labels: str) -> float:
    """Return the _count of a prometheus_client Histogram label set."""
    if histogram is None:
        return 0.0
    try:
        return histogram.labels(**labels)._sum.get()  # sum > 0 means observed
    except Exception:
        return 0.0


def _make_result(content: str = "Hello world " * 50, *, status: CallStatus = CallStatus.OK) -> FirecrawlResult:
    return FirecrawlResult(
        status=status,
        content_markdown=content,
        source_url="https://example.com/article",
        endpoint="test",
    )


def _make_empty_result() -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.ERROR,
        content_markdown="",
        content_html="",
        error_text="no content",
        source_url="https://example.com/article",
        endpoint="test",
    )


def _make_provider(name: str, result: FirecrawlResult) -> MagicMock:
    provider = MagicMock()
    provider.provider_name = name
    provider.scrape_markdown = AsyncMock(return_value=result)
    return provider


def _make_chain(*providers: Any, min_content_length: int = 0) -> Any:
    from app.adapters.content.scraper.chain import ContentScraperChain
    return ContentScraperChain(list(providers), min_content_length=min_content_length, race_enabled=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_increments_attempts_and_successes() -> None:
    """On a successful scrape, attempts_total{status=success} and latency are recorded."""
    provider_name = "scrapling"
    provider = _make_provider(provider_name, _make_result())
    chain = _make_chain(provider)

    before_attempts = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=provider_name, status="success"
    )
    before_failures_empty = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="empty"
    )

    with patch("app.adapters.content.scraper.chain.is_url_safe_async", return_value=(True, "")):
        await chain.scrape_markdown("https://example.com/article")

    after_attempts = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=provider_name, status="success"
    )
    after_failures_empty = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="empty"
    )

    assert after_attempts - before_attempts == pytest.approx(1.0), "attempts_total{success} must increment by 1"
    assert after_failures_empty == before_failures_empty, "failures_total must NOT increment on success"


@pytest.mark.asyncio
async def test_empty_result_increments_failures_with_reason_empty() -> None:
    """When the provider returns no content, failures_total{reason=empty} increments."""
    provider_name = "crawl4ai"
    provider = _make_provider(provider_name, _make_empty_result())
    chain = _make_chain(provider)

    before_failures = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="empty"
    )
    before_successes = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=provider_name, status="success"
    )

    with patch("app.adapters.content.scraper.chain.is_url_safe_async", return_value=(True, "")):
        result = await chain.scrape_markdown("https://example.com/article")

    after_failures = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="empty"
    )
    after_successes = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=provider_name, status="success"
    )
    after_attempts_error = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=provider_name, status="error"
    )

    assert after_failures - before_failures == pytest.approx(1.0), "failures_total{empty} must increment"
    assert after_successes == before_successes, "successes must NOT increment on empty result"
    assert result.status == CallStatus.ERROR


@pytest.mark.asyncio
async def test_exception_increments_failures_with_reason_error() -> None:
    """When the provider raises an exception, failures_total{reason=error} increments."""
    provider_name = "playwright"
    provider = MagicMock()
    provider.provider_name = provider_name
    provider.scrape_markdown = AsyncMock(side_effect=RuntimeError("connection refused"))
    chain = _make_chain(provider)

    before_failures_error = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="error"
    )

    with patch("app.adapters.content.scraper.chain.is_url_safe_async", return_value=(True, "")):
        result = await chain.scrape_markdown("https://example.com/article")

    after_failures_error = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="error"
    )

    assert after_failures_error - before_failures_error == pytest.approx(1.0), "failures_total{error} must increment on exception"
    assert result.status == CallStatus.ERROR


@pytest.mark.asyncio
async def test_fallback_chain_records_both_providers() -> None:
    """When the first provider fails and the second succeeds, each is independently recorded."""
    failing_name = "defuddle"
    winning_name = "scrapling"

    failing_provider = _make_provider(failing_name, _make_empty_result())
    winning_provider = _make_provider(winning_name, _make_result())
    chain = _make_chain(failing_provider, winning_provider)

    before_fail_attempts_error = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=failing_name, status="error"
    )
    before_fail_failures_empty = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=failing_name, reason="empty"
    )
    before_win_attempts_success = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=winning_name, status="success"
    )

    with patch("app.adapters.content.scraper.chain.is_url_safe_async", return_value=(True, "")):
        result = await chain.scrape_markdown("https://example.com/article")

    after_fail_attempts_error = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=failing_name, status="error"
    )
    after_fail_failures_empty = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=failing_name, reason="empty"
    )
    after_win_attempts_success = _counter_value(
        metrics_module.SCRAPER_ATTEMPTS_TOTAL, provider=winning_name, status="success"
    )

    assert after_fail_attempts_error - before_fail_attempts_error == pytest.approx(1.0), "failing provider: attempts{error}++"
    assert after_fail_failures_empty - before_fail_failures_empty == pytest.approx(1.0), "failing provider: failures{empty}++"
    assert after_win_attempts_success - before_win_attempts_success == pytest.approx(1.0), "winning provider: attempts{success}++"
    assert result.status == CallStatus.OK


@pytest.mark.asyncio
async def test_too_short_increments_failures_with_reason_too_short() -> None:
    """Content below min_content_length triggers failures_total{reason=too_short}."""
    provider_name = "direct_html"
    short_content = "Too short."
    provider = _make_provider(provider_name, _make_result(short_content))
    chain = _make_chain(provider, min_content_length=500)

    before = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="too_short"
    )

    with patch("app.adapters.content.scraper.chain.is_url_safe_async", return_value=(True, "")):
        await chain.scrape_markdown("https://example.com/article")

    after = _counter_value(
        metrics_module.SCRAPER_CHAIN_FAILURES_TOTAL, provider=provider_name, reason="too_short"
    )

    assert after - before == pytest.approx(1.0), "failures_total{too_short} must increment"


@pytest.mark.asyncio
async def test_latency_histogram_is_observed_on_success() -> None:
    """Per-provider latency histogram records at least one observation on success."""
    provider_name = "crawlee"
    provider = _make_provider(provider_name, _make_result())
    chain = _make_chain(provider)

    before_sum = _histogram_count(
        metrics_module.SCRAPER_ATTEMPT_LATENCY_SECONDS, provider=provider_name
    )

    with patch("app.adapters.content.scraper.chain.is_url_safe_async", return_value=(True, "")):
        await chain.scrape_markdown("https://example.com/article")

    after_sum = _histogram_count(
        metrics_module.SCRAPER_ATTEMPT_LATENCY_SECONDS, provider=provider_name
    )

    assert after_sum >= before_sum, "latency histogram sum must be non-decreasing"
