"""Tests for trending topics, related summaries, and duplicate check endpoints."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.api.routers.auth.tokens import create_access_token
from app.api.services.search_service import SearchService
from app.core.time_utils import UTC
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.models import Request, Summary, User

# ==================== Trending Topics Tests ====================


def _trending_payload(tags: list[dict] | None = None) -> dict:
    """The shape `_compute_trending` actually returns.

    `TrendingTopicsResponse` requires `tags` and `time_range`; a mock that
    invents its own keys makes the endpoint fail response validation instead of
    exercising it.
    """
    return {
        "tags": tags if tags is not None else [],
        "time_range": {"start": "2026-06-30T00:00:00Z", "end": "2026-07-30T00:00:00Z"},
    }


@patch("app.api.routers.content.search.get_trending_payload")
def test_get_trending_topics_success(mock_trending, client, search_token):
    """Test successful trending topics retrieval."""
    mock_trending.return_value = _trending_payload(
        [
            {"tag": "#ai", "count": 10, "trend": "up", "percentage_change": 50.0},
            {"tag": "#blockchain", "count": 8, "trend": "stable", "percentage_change": 2.0},
        ]
    )

    response = client.get(
        "/v1/topics/trending",
        params={"limit": 20, "days": 30},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["tags"]) == 2
    assert data["tags"][0]["tag"] == "#ai"
    # Serialized through serialization_alias, per the published contract.
    assert data["tags"][0]["percentageChange"] == 50.0
    assert "timeRange" in data


@patch("app.api.routers.content.search.get_trending_payload")
def test_get_trending_topics_with_custom_params(mock_trending, client, search_token):
    """Test trending topics with custom limit and days."""
    mock_trending.return_value = _trending_payload()

    response = client.get(
        "/v1/topics/trending",
        params={"limit": 10, "days": 7},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    # Verify function called with correct parameters
    mock_trending.assert_called_once()
    call_args = mock_trending.call_args
    assert call_args[1]["limit"] == 10
    assert call_args[1]["days"] == 7


def test_get_trending_topics_invalid_limit(client, search_token):
    """Test trending topics with invalid limit."""
    response = client.get(
        "/v1/topics/trending",
        params={"limit": 101, "days": 30},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 422


def test_get_trending_topics_invalid_days(client, search_token):
    """Test trending topics with invalid days parameter."""
    response = client.get(
        "/v1/topics/trending",
        params={"limit": 20, "days": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 422

    response = client.get(
        "/v1/topics/trending",
        params={"limit": 20, "days": 366},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 422


def test_get_trending_topics_unauthorized(client):
    """Test trending topics without authentication."""
    response = client.get(
        "/v1/topics/trending",
        params={"limit": 20, "days": 30},
    )

    assert response.status_code == 401


# ==================== Related Summaries Tests ====================


def test_get_related_summaries_success(client, search_data, search_token):
    """Test successful related summaries retrieval."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "ai", "limit": 20, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert "tag" in data
    assert "summaries" in data
    assert "pagination" in data
    # Tag should be normalized with #
    assert data["tag"] == "#ai"


def test_get_related_summaries_with_hash(client, search_data, search_token):
    """Test related summaries with hashtag already included."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "#blockchain", "limit": 20, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tag"] == "#blockchain"


def test_get_related_summaries_no_matches(client, search_data, search_token):
    """Test related summaries with no matching tag."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "nonexistent", "limit": 20, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["summaries"]) == 0


def test_get_related_summaries_case_insensitive(client, search_data, search_token):
    """Test that tag matching is case insensitive."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "AI", "limit": 20, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    # Should match #ai tag despite different case
    assert data["tag"] == "#AI"


def test_get_related_summaries_pagination(client, search_data, search_token):
    """Test related summaries pagination."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "ai", "limit": 1, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["pagination"]["limit"] == 1
    assert data["pagination"]["offset"] == 0


def test_get_related_summaries_unauthorized(client):
    """Test related summaries without authentication."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "test", "limit": 20, "offset": 0},
    )

    assert response.status_code == 401


def test_get_related_summaries_empty_tag(client, search_token):
    """Test related summaries with empty tag."""
    response = client.get(
        "/v1/topics/related",
        params={"tag": "", "limit": 20, "offset": 0},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 422


# ==================== Duplicate Check Tests ====================


def test_check_duplicate_not_duplicate(client, search_user, search_token):
    """Test URL that is not a duplicate."""
    with patch.object(
        SearchService,
        "check_duplicate",
        AsyncMock(
            return_value={
                "is_duplicate": False,
                "normalized_url": "https://newsite.com/article",
                "dedupe_hash": "abc123",
            }
        ),
    ):
        response = client.get(
            "/v1/urls/check-duplicate",
            params={"url": "https://newsite.com/article", "include_summary": False},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    # The service speaks snake_case; the response model serializes camelCase.
    assert data["isDuplicate"] is False
    assert "normalizedUrl" in data
    assert "dedupeHash" in data


def test_check_duplicate_is_duplicate(
    client,
    search_data,
    search_user,
    search_token,
):
    """Test URL that is a duplicate."""
    existing_url = search_data[0]["request"].input_url

    with patch.object(
        SearchService,
        "check_duplicate",
        AsyncMock(
            return_value={
                "is_duplicate": True,
                "request_id": search_data[0]["request"].id,
                "summary_id": search_data[0]["summary"].id,
                "summarized_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        ),
    ):
        response = client.get(
            "/v1/urls/check-duplicate",
            params={"url": existing_url, "include_summary": False},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["isDuplicate"] is True
    assert "requestId" in data
    assert "summaryId" in data
    assert "summarizedAt" in data


def test_check_duplicate_with_summary(
    client,
    search_data,
    search_user,
    search_token,
):
    """Test duplicate check with summary details included."""
    existing_url = search_data[0]["request"].input_url

    with patch.object(
        SearchService,
        "check_duplicate",
        AsyncMock(
            return_value={
                "is_duplicate": True,
                "request_id": search_data[0]["request"].id,
                "summary_id": search_data[0]["summary"].id,
                "summarized_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "summary": {
                    "title": "Introduction to AI",
                    "tldr": "AI is transforming technology",
                    "url": existing_url,
                },
            }
        ),
    ):
        response = client.get(
            "/v1/urls/check-duplicate",
            params={"url": existing_url, "include_summary": True},
            headers={"Authorization": f"Bearer {search_token}"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["isDuplicate"] is True
    assert "summary" in data
    assert "title" in data["summary"]
    assert "tldr" in data["summary"]
    assert "url" in data["summary"]


@pytest.mark.asyncio
async def test_check_duplicate_url_normalization(client, search_data, search_token, db):
    """Test that URL normalization works in duplicate detection."""
    # Original URL
    original_url = "https://example.com/ai-article"
    # URL with trailing slash and query params (should normalize to same)
    variant_url = "https://example.com/ai-article/?utm_source=test"

    # First, create a request with normalized original
    normalized = normalize_url(original_url)
    dedupe_hash = compute_dedupe_hash(normalized)

    async with db.transaction() as session:
        req = Request(
            user_id=search_data[0]["request"].user_id,
            type="url",
            status="completed",
            input_url=original_url,
            normalized_url=normalized,
            dedupe_hash=dedupe_hash,
        )
        session.add(req)
        await session.flush()
        session.add(
            Summary(
                request_id=req.id,
                lang="en",
                json_payload={"tldr": "test"},
            )
        )
    await db.engine.dispose()

    # Check variant URL
    response = client.get(
        "/v1/urls/check-duplicate",
        params={"url": variant_url, "include_summary": False},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 200


def test_check_duplicate_unauthorized(client):
    """Test duplicate check without authentication."""
    response = client.get(
        "/v1/urls/check-duplicate",
        params={"url": "https://example.com/test", "include_summary": False},
    )

    assert response.status_code == 401


def test_check_duplicate_short_url(client, search_token):
    """Test duplicate check with URL below minimum length."""
    response = client.get(
        "/v1/urls/check-duplicate",
        params={"url": "http://a", "include_summary": False},
        headers={"Authorization": f"Bearer {search_token}"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_check_duplicate_different_user(client, search_data, search_user, db, monkeypatch):
    """Test that duplicate check respects user isolation."""
    other_user_id = 111222333
    async with db.transaction() as session:
        session.add(User(telegram_user_id=other_user_id, username="other_user"))
    await db.engine.dispose()

    # Add both users to ALLOWED_USER_IDS
    monkeypatch.setenv("ALLOWED_USER_IDS", f"{search_user.telegram_user_id},{other_user_id}")

    other_token = create_access_token(other_user_id, client_id="test")

    # Check URL that exists for first user
    existing_url = search_data[0]["request"].input_url

    with patch.object(
        SearchService,
        "check_duplicate",
        AsyncMock(
            return_value={
                "is_duplicate": False,
                "normalized_url": existing_url,
                "dedupe_hash": "other-user-hash",
            }
        ),
    ):
        response = client.get(
            "/v1/urls/check-duplicate",
            params={"url": existing_url, "include_summary": False},
            headers={"Authorization": f"Bearer {other_token}"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    # Should not be duplicate for different user
    assert data["isDuplicate"] is False
