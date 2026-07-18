"""Tests for user stats endpoint with ensure_mapping safety."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

# Mock redis before any app imports
sys.modules["redis"] = MagicMock()
sys.modules["redis.asyncio"] = MagicMock()

from app.api.routers import auth
from app.api.routers.user.user import get_user_stats
from app.db.models import Request, Summary, User

if TYPE_CHECKING:
    from app.db.session import Database


def _configure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-characters-long-123456")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "com.example.app")
    auth._cfg = None  # type: ignore[attr-defined]


async def _create_user_request_summary(
    db: Database,
    *,
    user_id: int,
    username: str,
    url: str,
    json_payload,
) -> int:
    """Insert a User, Request, and Summary; return the summary id."""
    payload = json_payload if isinstance(json_payload, dict) else {}
    reading_time = payload.get("estimated_reading_time_min")
    topic_tags = payload.get("topic_tags")
    async with db.transaction() as session:
        session.add(User(telegram_user_id=user_id, username=username))
        await session.flush()
        request = Request(
            user_id=user_id,
            input_url=url,
            normalized_url=url,
            status="completed",
            type="url",
        )
        session.add(request)
        await session.flush()
        summary = Summary(
            request_id=request.id,
            lang="en",
            json_payload=json_payload,
            reading_time=reading_time if isinstance(reading_time, int) else None,
            topic_tags=topic_tags if isinstance(topic_tags, list) else None,
        )
        session.add(summary)
        await session.flush()
        return int(summary.id)


@pytest.mark.asyncio
async def test_user_stats_with_valid_json_payload(db: Database, monkeypatch: pytest.MonkeyPatch):
    """Test user stats with properly formatted json_payload."""
    _configure_env(monkeypatch)

    await _create_user_request_summary(
        db,
        user_id=123456789,
        username="testuser",
        url="http://test.com",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech", "ai"],
            "metadata": {"title": "Test Article", "domain": "test.com"},
        },
    )

    response = await get_user_stats(user={"user_id": 123456789})

    assert response["data"]["totalSummaries"] == 1
    assert response["data"]["totalReadingTimeMin"] == 5


@pytest.mark.asyncio
async def test_user_stats_with_none_json_payload(db: Database, monkeypatch: pytest.MonkeyPatch):
    """Test user stats handles None json_payload gracefully."""
    _configure_env(monkeypatch)

    await _create_user_request_summary(
        db,
        user_id=123456790,
        username="testuser2",
        url="http://test2.com",
        json_payload=None,
    )

    response = await get_user_stats(user={"user_id": 123456790})

    assert response["data"]["totalSummaries"] == 1
    assert response["data"]["totalReadingTimeMin"] == 0


@pytest.mark.asyncio
async def test_user_stats_with_string_json_payload(db: Database, monkeypatch: pytest.MonkeyPatch):
    """Test user stats handles string json_payload (legacy data) gracefully."""
    _configure_env(monkeypatch)

    summary_id = await _create_user_request_summary(
        db,
        user_id=123456791,
        username="testuser3",
        url="http://test3.com",
        json_payload={},
    )

    # Simulate legacy data: a JSON-encoded string in a JSONB column.
    # JSONB rejects raw strings as the column value, but ensure_mapping in
    # the user-stats reader is supposed to tolerate it. Encode and store
    # via TEXT cast so the value reaches the reader as a string.
    from sqlalchemy import text as sql_text

    async with db.transaction() as session:
        await session.execute(
            sql_text(
                "UPDATE summaries SET json_payload = "
                'to_jsonb(\'{"estimated_reading_time_min": 10, '
                '"topic_tags": ["python"]}\'::text) '
                "WHERE id = :sid"
            ),
            {"sid": summary_id},
        )

    response = await get_user_stats(user={"user_id": 123456791})

    assert response["data"]["totalSummaries"] == 1


@pytest.mark.asyncio
async def test_user_stats_with_invalid_topic_tags(db: Database, monkeypatch: pytest.MonkeyPatch):
    """Test user stats handles invalid topic_tags type gracefully."""
    _configure_env(monkeypatch)

    await _create_user_request_summary(
        db,
        user_id=123456792,
        username="testuser4",
        url="http://test4.com",
        json_payload={
            "estimated_reading_time_min": 3,
            "topic_tags": "not-a-list",  # invalid: should be list
            "metadata": {"title": "Test"},
        },
    )

    response = await get_user_stats(user={"user_id": 123456792})

    assert response["data"]["totalSummaries"] == 1
    assert response["data"]["favoriteTopics"] == []
