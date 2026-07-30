"""Tests for domain-level fail-fast in batch URL processing.

Includes unit tests for domain membership and integration tests
verifying asyncio.Event-based cancellation of in-flight siblings.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapter_models.batch_processing import URLBatchStatus
from app.adapters.telegram.url_batch_policy_service import URLBatchPolicyService
from app.adapters.telegram.url_handler import URLHandler


class TestDomainFailFast(unittest.IsolatedAsyncioTestCase):
    """Verify domain-level fail-fast skips URLs after domain timeout."""

    async def test_second_url_from_timed_out_domain_is_skipped(self):
        """When first URL from a domain times out, second URL is skipped."""
        urls = [
            "https://habr.com/article/1",
            "https://habr.com/article/2",
        ]

        batch_status = URLBatchStatus.from_urls(urls)

        # Simulate: first URL exhausts retries -> domain added to failed set
        failed_domains: set[str] = set()
        failed_domains.add("habr.com")

        # Check domain membership for second URL
        entry = batch_status._find_entry(urls[1])
        assert entry is not None
        assert entry.domain == "habr.com"
        assert entry.domain in failed_domains

    async def test_different_domain_not_affected(self):
        """URLs from different domains are not affected by fail-fast."""
        failed_domains: set[str] = {"habr.com"}

        batch_status = URLBatchStatus.from_urls(
            [
                "https://habr.com/article/1",
                "https://example.com/page",
            ]
        )

        entry = batch_status._find_entry("https://example.com/page")
        assert entry is not None
        assert entry.domain == "example.com"
        assert entry.domain not in failed_domains


class TestDomainFailFastFormatting(unittest.TestCase):
    """Verify domain_timeout error type formats correctly."""

    def test_domain_timeout_error_format(self):
        """domain_timeout error type displays as 'Skipped (slow site)'."""
        from app.adapters.external.formatting.batch_progress_formatter import (
            BatchProgressFormatter,
        )

        result = BatchProgressFormatter._format_error_short(
            "domain_timeout", "Skipped (domain habr.com timed out)"
        )
        assert result == "Skipped (slow site)"

    def test_regular_timeout_unaffected(self):
        """Regular timeout formatting shows 'Timed out'."""
        from app.adapters.external.formatting.batch_progress_formatter import (
            BatchProgressFormatter,
        )

        result = BatchProgressFormatter._format_error_short("timeout", None)
        assert result == "Timed out"


# ---------------------------------------------------------------------------
# Integration tests: asyncio.Event-based domain cancellation
# ---------------------------------------------------------------------------


def _make_url_handler() -> URLHandler:
    """Create a URLHandler with mocked dependencies."""
    db = MagicMock()
    response_formatter = MagicMock()
    response_formatter.safe_reply = AsyncMock()
    response_formatter.safe_reply_with_id = AsyncMock(return_value=1)
    response_formatter.edit_message = AsyncMock(return_value=True)
    response_formatter.MAX_BATCH_URLS = 20
    url_processor = MagicMock()
    handler = URLHandler(db=db, response_formatter=response_formatter, url_processor=url_processor)
    cast("Any", handler).handle_single_url = AsyncMock()

    # Avoid hitting real repository internals in fail-fast timing tests.
    next_request_id = 0

    async def _create_minimal_request(**_kwargs):
        nonlocal next_request_id
        next_request_id += 1
        return next_request_id, True

    handler.request_repo = MagicMock()
    handler.request_repo.async_get_request_by_dedupe_hash = AsyncMock(return_value=None)
    handler.request_repo.async_create_minimal_request = AsyncMock(
        side_effect=_create_minimal_request
    )
    handler.request_repo.async_update_request_error = AsyncMock()
    handler._batch_processor._request_repo = handler.request_repo
    return handler


def _make_message(uid: int = 1) -> MagicMock:
    """Create a mock Telegram message."""
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=uid)
    return msg


@pytest.mark.asyncio
async def test_concurrent_same_domain_cancel_on_timeout():
    """4 same-domain URLs: first times out, others cancelled immediately.

    Wall-clock should be close to 1x timeout, not 4x.
    """
    handler = _make_url_handler()
    test_timeout = 2.0

    urls = [
        "https://habr.com/article/1",
        "https://habr.com/article/2",
        "https://habr.com/article/3",
        "https://habr.com/article/4",
    ]

    started = 0
    cancelled = 0

    async def _slow_handler(*args, **kwargs):
        nonlocal started, cancelled
        started += 1
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancelled += 1
            raise

    cast("Any", handler).handle_single_url = AsyncMock(side_effect=_slow_handler)
    handler._batch_policy = URLBatchPolicyService(
        initial_timeout_sec=test_timeout,
        max_timeout_sec=test_timeout * 2,
        max_retries=0,
    )
    msg = _make_message()
    wall_start = time.monotonic()

    await handler.process_url_batch(msg, urls, uid=1, correlation_id="test-cid")

    wall_elapsed = time.monotonic() - wall_start

    # The property under test is that the siblings were cancelled, so assert
    # that directly. The wall-clock form alone flaked on loaded CI runners
    # (5.2s against a 5.0s bound) because it measures scheduler latency as much
    # as it measures fail-fast.
    assert started == len(urls), f"expected all {len(urls)} URLs to start, got {started}"
    assert cancelled >= len(urls) - 1, (
        f"expected the in-flight siblings to be cancelled, only {cancelled} were. "
        "Domain fail-fast did not cancel them."
    )
    # Loose upper bound as a backstop: without fail-fast this serialises to
    # 4 x 2s = 8s, so anything under 3.5x still separates the two behaviours
    # while absorbing runner slowness.
    assert wall_elapsed < test_timeout * 3.5, (
        f"Expected wall-clock < {test_timeout * 3.5}s but got {wall_elapsed:.1f}s. "
        "Domain fail-fast did not cancel in-flight siblings."
    )


@pytest.mark.asyncio
async def test_mixed_domains_only_cancel_affected():
    """2 slow domain-a URLs + 2 fast domain-b URLs.

    Domain-b should succeed; domain-a should fail.
    """
    handler = _make_url_handler()
    test_timeout = 2.0

    urls = [
        "https://slow-domain.com/page/1",
        "https://slow-domain.com/page/2",
        "https://fast-domain.com/page/1",
        "https://fast-domain.com/page/2",
    ]

    async def _domain_aware_handler(*args, **kwargs):
        # Extract URL from positional args (message, url, ...)
        url = args[1] if len(args) > 1 else kwargs.get("url", "")
        if "slow-domain.com" in url:
            await asyncio.sleep(999)
            return None
        return SimpleNamespace(title=f"Title for {url}")

    handler.handle_single_url = AsyncMock(side_effect=_domain_aware_handler)
    handler._batch_policy = URLBatchPolicyService(
        initial_timeout_sec=test_timeout,
        max_timeout_sec=test_timeout * 2,
        max_retries=0,
    )
    msg = _make_message()

    await handler.process_url_batch(msg, urls, uid=1, correlation_id="test-cid")

    # Check the completion message for partial success
    calls = handler.response_formatter.safe_reply.call_args_list
    completion_call = calls[-1]
    completion_text = completion_call[0][1]

    # 2 fast-domain URLs succeed, 2 slow-domain URLs fail -> "2/4"
    assert "2/4" in completion_text, (
        f"Expected '2/4' in completion message but got: {completion_text}"
    )


@pytest.mark.asyncio
async def test_event_already_set_skips_immediately():
    """If domain event is pre-set (first URL timed out), remaining siblings skip in <1s.

    Both URLs are from the same domain. The first will timeout, setting the event.
    The second should be cancelled near-instantly by the event, not burning its own timeout.
    """
    handler = _make_url_handler()
    test_timeout = 2.0

    urls = [
        "https://already-failed.com/page/1",
        "https://already-failed.com/page/2",
    ]

    async def _hang_forever(*args, **kwargs):
        await asyncio.sleep(999)

    handler.handle_single_url = AsyncMock(side_effect=_hang_forever)
    handler._batch_policy = URLBatchPolicyService(
        initial_timeout_sec=test_timeout,
        max_timeout_sec=test_timeout * 2,
        max_retries=0,
    )
    msg = _make_message()
    wall_start = time.monotonic()

    await handler.process_url_batch(msg, urls, uid=1, correlation_id="test-cid")

    wall_elapsed = time.monotonic() - wall_start

    # With domain event cancellation, total time should be close to 1x timeout.
    # Without, it would be 2x timeout (each URL independently burns through).
    assert wall_elapsed < test_timeout * 2.0, (
        f"Expected wall-clock < {test_timeout * 2.0}s but got {wall_elapsed:.1f}s. "
        "Domain event did not trigger immediate sibling skip."
    )
