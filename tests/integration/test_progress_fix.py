#!/usr/bin/env python3
"""Test script to verify progress message editing works correctly.
This tests the fix for progress messages being sent as new messages instead of editing existing ones.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external.response_formatter import ResponseFormatter

# Marked so the integration CI job (`-m integration`) collects this suite. Files
# under tests/integration/ are --ignored by the unit job, so an unmarked file
# here runs in neither job and is silently excluded. See
# tests/test_ci_workflow_excludes.py.
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_progress_message_editing():
    """Test that progress messages use edit_message when message_id is provided."""
    # Create mock message
    mock_message = Mock()
    mock_message.chat.id = 123456789

    # Create mock telegram client
    mock_client = AsyncMock()
    mock_client.edit_message_text = AsyncMock()

    # Create response formatter
    rf = ResponseFormatter()
    telegram_client = Mock()
    telegram_client.client = mock_client
    rf.set_telegram_client(telegram_client)

    # Mock the send_message method to return a message ID
    mock_client.send_message = AsyncMock(return_value=Mock(id=987654321))

    # Test 2: edit_message should be called when message_id is provided
    progress_text = "🔄 Processing links: 2/5\n██████████░░░░░░░░░░"

    # Call edit_message directly
    await rf.edit_message(mock_message.chat.id, 987654321, progress_text)

    # Verify edit_message_text was called
    mock_client.edit_message_text.assert_called_once_with(
        chat_id=mock_message.chat.id, message_id=987654321, text=progress_text
    )


@pytest.mark.asyncio
async def test_progress_message_updater_passes_html_parse_mode() -> None:
    """Regression: every periodic update must reach the tracker with parse_mode set.

    The progress formatters emit HTML; without parse_mode, Telegram renders the
    raw tags (``<b>Content Extraction</b>``) as literal text instead of bold.
    """
    from app.utils.progress_message_updater import ProgressMessageUpdater

    tracker = AsyncMock()
    message = Mock()
    updater = ProgressMessageUpdater(tracker, message, update_interval=0.05)

    await updater.start(lambda elapsed: f"<b>Working</b> ({elapsed:.0f}s)")
    await asyncio.sleep(0.12)
    await updater.finalize("<b>Done</b>")

    assert tracker.update.await_count >= 1
    for call in tracker.update.await_args_list:
        assert call.kwargs.get("parse_mode") == "HTML"
    assert tracker.finalize.await_count == 1
    assert tracker.finalize.await_args.kwargs.get("parse_mode") == "HTML"


if __name__ == "__main__":
    asyncio.run(test_progress_message_editing())
