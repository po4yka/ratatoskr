from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.application.use_cases.get_unread_summaries import (
    GetUnreadSummariesQuery,
    GetUnreadSummariesUseCase,
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
async def test_get_unread_summaries_happy_path() -> None:
    repo = AsyncMock()
    repo.async_get_unread_summaries.return_value = [
        _make_summary_dict(1),
        _make_summary_dict(2),
    ]
    use_case = GetUnreadSummariesUseCase(summary_repository=repo)
    query = GetUnreadSummariesQuery(user_id=10, chat_id=20, limit=5)

    result = await use_case.execute(query)

    assert len(result) == 2
    repo.async_get_unread_summaries.assert_awaited_once_with(
        user_id=10, chat_id=20, limit=5, topic=None
    )


@pytest.mark.asyncio
async def test_get_unread_summaries_empty() -> None:
    repo = AsyncMock()
    repo.async_get_unread_summaries.return_value = []
    use_case = GetUnreadSummariesUseCase(summary_repository=repo)
    query = GetUnreadSummariesQuery(user_id=1, chat_id=2, limit=10)

    result = await use_case.execute(query)

    assert result == []
    repo.async_get_unread_summaries.assert_awaited_once_with(
        user_id=1, chat_id=2, limit=10, topic=None
    )


@pytest.mark.asyncio
async def test_get_unread_summaries_with_topic() -> None:
    repo = AsyncMock()
    repo.async_get_unread_summaries.return_value = [_make_summary_dict(3)]
    use_case = GetUnreadSummariesUseCase(summary_repository=repo)
    query = GetUnreadSummariesQuery(user_id=1, chat_id=2, limit=10, topic="tech")

    result = await use_case.execute(query)

    assert len(result) == 1
    assert result[0]["id"] == 3
    repo.async_get_unread_summaries.assert_awaited_once_with(
        user_id=1, chat_id=2, limit=10, topic="tech"
    )


def test_query_validation_rejects_invalid_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        GetUnreadSummariesQuery(user_id=0, chat_id=1)


def test_query_validation_rejects_limit_over_100() -> None:
    with pytest.raises(ValueError, match="limit"):
        GetUnreadSummariesQuery(user_id=1, chat_id=1, limit=101)


def test_query_validation_rejects_empty_topic() -> None:
    with pytest.raises(ValueError, match="topic"):
        GetUnreadSummariesQuery(user_id=1, chat_id=1, topic="   ")
