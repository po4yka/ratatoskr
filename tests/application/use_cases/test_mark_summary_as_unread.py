from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.application.use_cases.mark_summary_as_unread import (
    MarkSummaryAsUnreadCommand,
    MarkSummaryAsUnreadUseCase,
)
from app.domain.events.summary_events import SummaryMarkedAsUnread
from app.domain.exceptions.domain_exceptions import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
)


def _make_summary_dict(summary_id: int, is_read: bool = True) -> dict:
    return {
        "id": summary_id,
        "request_id": summary_id * 10,
        "lang": "en",
        "json_payload": {"summary_250": "text", "summary_1000": "text"},
        "insights_json": None,
        "is_read": is_read,
        "version": 1,
        "created_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_mark_as_unread_happy_path() -> None:
    repo = AsyncMock()
    repo.async_get_summary_by_id = AsyncMock(return_value=_make_summary_dict(7, is_read=True))
    repo.async_mark_summary_as_unread = AsyncMock()
    use_case = MarkSummaryAsUnreadUseCase(summary_repository=repo)
    command = MarkSummaryAsUnreadCommand(summary_id=7, user_id=2)

    event = await use_case.execute(command)

    assert isinstance(event, SummaryMarkedAsUnread)
    assert event.summary_id == 7
    repo.async_mark_summary_as_unread.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_mark_as_unread_not_found_raises() -> None:
    repo = AsyncMock()
    repo.async_get_summary_by_id = AsyncMock(return_value=None)
    use_case = MarkSummaryAsUnreadUseCase(summary_repository=repo)
    command = MarkSummaryAsUnreadCommand(summary_id=99, user_id=1)

    with pytest.raises(ResourceNotFoundError):
        await use_case.execute(command)


@pytest.mark.asyncio
async def test_mark_as_unread_already_unread_raises() -> None:
    repo = AsyncMock()
    repo.async_get_summary_by_id = AsyncMock(return_value=_make_summary_dict(7, is_read=False))
    use_case = MarkSummaryAsUnreadUseCase(summary_repository=repo)
    command = MarkSummaryAsUnreadCommand(summary_id=7, user_id=2)

    with pytest.raises(InvalidStateTransitionError):
        await use_case.execute(command)


def test_command_validation_rejects_invalid_summary_id() -> None:
    with pytest.raises(ValueError, match="summary_id"):
        MarkSummaryAsUnreadCommand(summary_id=-1, user_id=1)
