from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.application.use_cases.mark_summary_as_read import (
    MarkSummaryAsReadCommand,
    MarkSummaryAsReadUseCase,
)
from app.domain.events.summary_events import SummaryMarkedAsRead
from app.domain.exceptions.domain_exceptions import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
)


def _make_summary_dict(summary_id: int, is_read: bool = False) -> dict:
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
async def test_mark_as_read_happy_path() -> None:
    repo = AsyncMock()
    repo.async_get_summary_by_request.return_value = None
    repo.async_get_summary_by_id = AsyncMock(return_value=_make_summary_dict(5, is_read=False))
    repo.async_mark_summary_as_read = AsyncMock()
    use_case = MarkSummaryAsReadUseCase(summary_repository=repo)
    command = MarkSummaryAsReadCommand(summary_id=5, user_id=1)

    event = await use_case.execute(command)

    assert isinstance(event, SummaryMarkedAsRead)
    assert event.summary_id == 5
    repo.async_mark_summary_as_read.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_mark_as_read_not_found_raises() -> None:
    repo = AsyncMock()
    repo.async_get_summary_by_id = AsyncMock(return_value=None)
    use_case = MarkSummaryAsReadUseCase(summary_repository=repo)
    command = MarkSummaryAsReadCommand(summary_id=99, user_id=1)

    with pytest.raises(ResourceNotFoundError):
        await use_case.execute(command)


@pytest.mark.asyncio
async def test_mark_as_read_already_read_raises() -> None:
    repo = AsyncMock()
    repo.async_get_summary_by_id = AsyncMock(return_value=_make_summary_dict(5, is_read=True))
    use_case = MarkSummaryAsReadUseCase(summary_repository=repo)
    command = MarkSummaryAsReadCommand(summary_id=5, user_id=1)

    with pytest.raises(InvalidStateTransitionError):
        await use_case.execute(command)


def test_command_validation_rejects_invalid_summary_id() -> None:
    with pytest.raises(ValueError, match="summary_id"):
        MarkSummaryAsReadCommand(summary_id=0, user_id=1)


def test_command_validation_rejects_invalid_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        MarkSummaryAsReadCommand(summary_id=1, user_id=0)
