from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.routers.social import rss


class _FakeRSSRepository:
    def __init__(self, *, subscription: dict | None) -> None:
        self.async_get_subscription_by_feed = AsyncMock(return_value=subscription)
        self.async_get_feed = AsyncMock(
            return_value={
                "id": 77,
                "url": "https://example.com/feed.xml",
                "etag": None,
                "last_modified": None,
            }
        )
        self.async_list_feed_items = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "guid": "guid-1",
                    "title": "Post",
                    "url": "https://example.com/post",
                    "author": None,
                    "published_at": None,
                    "created_at": None,
                }
            ]
        )
        self.async_create_feed_item = AsyncMock(return_value={"id": 2})
        self.async_create_feed_items = AsyncMock(return_value=[{"id": 2}])
        self.async_update_feed = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_list_feed_items_rejects_unsubscribed_feed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRSSRepository(subscription=None)
    monkeypatch.setattr(rss, "_get_rss_repo", lambda: repo)

    with pytest.raises(ResourceNotFoundError):
        await rss.list_feed_items(feed_id=77, limit=20, offset=0, user={"user_id": 1001})

    repo.async_get_subscription_by_feed.assert_awaited_once_with(user_id=1001, feed_id=77)
    repo.async_list_feed_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_feed_items_allows_subscribed_feed_id(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRSSRepository(subscription={"id": 11, "user": 1001, "feed": 77})
    monkeypatch.setattr(rss, "_get_rss_repo", lambda: repo)

    response = await rss.list_feed_items(feed_id=77, limit=20, offset=0, user={"user_id": 1001})

    repo.async_get_subscription_by_feed.assert_awaited_once_with(user_id=1001, feed_id=77)
    repo.async_list_feed_items.assert_awaited_once_with(77, limit=20, offset=0)
    assert response["success"] is True
    assert response["data"]["items"][0]["guid"] == "guid-1"


@pytest.mark.asyncio
async def test_refresh_feed_rejects_unsubscribed_feed_id_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRSSRepository(subscription=None)
    fetch_feed = Mock()
    monkeypatch.setattr(rss, "_get_rss_repo", lambda: repo)
    monkeypatch.setattr("app.adapters.rss.feed_fetcher.fetch_feed", fetch_feed)

    with pytest.raises(ResourceNotFoundError):
        await rss.refresh_feed(feed_id=77, user={"user_id": 1002})

    repo.async_get_subscription_by_feed.assert_awaited_once_with(user_id=1002, feed_id=77)
    repo.async_get_feed.assert_not_awaited()
    fetch_feed.assert_not_called()
