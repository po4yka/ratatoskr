"""Tests that URLHandler.handle_document_file preserves cancellation semantics."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from app.adapters.telegram.url_handler import URLHandler

_DOWNLOAD_TARGET = "app.adapters.telegram.url_handler.URLHandler._download_document_file"
_SLEEP_TARGET = "app.adapters.telegram.url_handler.asyncio.sleep"
_PROCESS_TARGET = "app.adapters.telegram.url_handler.URLHandler.process_url_batch"


def _make_handler() -> URLHandler:
    """Build a minimal URLHandler accepted by handle_document_file."""
    response_formatter = cast(
        "Any",
        SimpleNamespace(
            safe_reply=AsyncMock(),
            safe_reply_with_id=AsyncMock(return_value=42),
            send_error_notification=AsyncMock(),
            MIN_MESSAGE_INTERVAL_MS=100,
            MAX_BATCH_URLS=50,
            _validate_url=MagicMock(return_value=(True, None)),
            sender=SimpleNamespace(is_draft_streaming_enabled=MagicMock(return_value=False)),
        ),
    )
    url_processor = cast(
        "Any", SimpleNamespace(handle_url_flow=AsyncMock(), summary_repo=None, audit_func=None)
    )
    file_validator = MagicMock()
    file_validator.cleanup_file = MagicMock()
    # A .txt upload whose contents are a bare URL list routes to the batch path,
    # where the rate-limit sleeps under test live.
    file_validator.safe_read_text_file = MagicMock(return_value=["https://example.com"])
    return URLHandler(
        db=cast("Any", SimpleNamespace()),
        response_formatter=response_formatter,
        url_processor=url_processor,
        file_validator=file_validator,
    )


class TestHandleDocumentFileCancellation(unittest.IsolatedAsyncioTestCase):
    """CancelledError from rate-limit sleeps must propagate, not be swallowed."""

    async def test_initial_sleep_propagates_cancelled_error(self) -> None:
        """CancelledError from the first asyncio.sleep (initial_gap) must propagate."""
        handler = _make_handler()
        message = AsyncMock()

        with (
            patch(_DOWNLOAD_TARGET, new_callable=AsyncMock, return_value="/tmp/fake.txt"),
            patch(_SLEEP_TARGET, side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await handler.handle_document_file(message, "cid-test", 1, 0.0)

    async def test_post_processing_sleep_propagates_cancelled_error(self) -> None:
        """CancelledError from the second asyncio.sleep (min_gap_sec) must propagate."""
        handler = _make_handler()
        message = AsyncMock()

        call_count = 0

        async def _selective_sleep(delay: float) -> None:
            nonlocal call_count
            _ = delay
            call_count += 1
            if call_count == 1:
                return
            raise asyncio.CancelledError()

        with (
            patch(_DOWNLOAD_TARGET, new_callable=AsyncMock, return_value="/tmp/fake.txt"),
            patch(_PROCESS_TARGET, new_callable=AsyncMock),
            patch(_SLEEP_TARGET, side_effect=_selective_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await handler.handle_document_file(message, "cid-test", 1, 0.0)

    async def test_regular_exceptions_still_handled(self) -> None:
        """Non-cancellation exceptions in sleep blocks should still be swallowed."""
        handler = _make_handler()
        message = AsyncMock()

        with (
            patch(_DOWNLOAD_TARGET, new_callable=AsyncMock, return_value="/tmp/fake.txt"),
            patch(_SLEEP_TARGET, side_effect=RuntimeError("timer glitch")),
        ):
            await handler.handle_document_file(message, "cid-test", 1, 0.0)


if __name__ == "__main__":
    unittest.main()
