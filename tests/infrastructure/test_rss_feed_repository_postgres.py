from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    ChannelCategory,
    FeedItem,
    RSSFeed,
    RSSFeedItem,
    RSSFeedSubscription,
    RSSItemDelivery,
    Source,
    Subscription,
    User,
)
from app.db.session import Database
from app.infrastructure.persistence.repositories.rss_feed_repository import (
    RSSFeedRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(FeedItem))
        await session.execute(delete(Subscription))
        await session.execute(delete(Source))
        await session.execute(delete(RSSItemDelivery))
        await session.execute(delete(RSSFeedSubscription))
        await session.execute(delete(RSSFeedItem))
        await session.execute(delete(RSSFeed))
        await session.execute(delete(ChannelCategory))
        await session.execute(delete(User))


async def _create_user(database: Database, *, telegram_user_id: int, username: str) -> User:
    async with database.transaction() as session:
        user = User(telegram_user_id=telegram_user_id, username=username)
        session.add(user)
        await session.flush()
        return user


@pytest.mark.asyncio
async def test_rss_feed_repository_subscriptions_preserve_join_shapes(
    database: Database,
) -> None:
    repo = RSSFeedRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=8101, username="rss-owner")

    async with database.transaction() as session:
        category = ChannelCategory(user_id=owner.telegram_user_id, name="News", position=1)
        session.add(category)
        await session.flush()

    feed = await repo.async_get_or_create_feed("https://example.com/feed.xml")
    duplicate = await repo.async_get_or_create_feed("https://example.com/feed.xml")
    assert duplicate["id"] == feed["id"]

    await repo.async_update_feed(
        int(feed["id"]),
        title="Example Feed",
        description="Articles",
        site_url="https://example.com",
    )
    subscription = await repo.async_create_subscription(
        user_id=owner.telegram_user_id,
        feed_id=int(feed["id"]),
        category_id=category.id,
    )
    existing = await repo.async_create_subscription(
        user_id=owner.telegram_user_id,
        feed_id=int(feed["id"]),
        category_id=category.id,
    )
    assert existing["id"] == subscription["id"]
    assert existing["user"] == owner.telegram_user_id
    assert existing["feed"] == feed["id"]
    assert existing["category"] == category.id

    listed = await repo.async_list_user_subscriptions(owner.telegram_user_id)
    assert listed == [
        {
            **listed[0],
            "feed_title": "Example Feed",
            "feed_url": "https://example.com/feed.xml",
            "site_url": "https://example.com",
            "feed_description": "Articles",
            "category_name": "News",
        }
    ]

    active = await repo.async_list_user_active_subscriptions(owner.telegram_user_id)
    assert active[0]["feed"]["url"] == "https://example.com/feed.xml"
    assert active[0]["category_name"] == "News"

    by_feed = await repo.async_get_subscription_by_feed(
        user_id=owner.telegram_user_id,
        feed_id=int(feed["id"]),
    )
    assert by_feed is not None
    assert by_feed["feed"]["title"] == "Example Feed"

    by_id = await repo.async_get_subscription(
        user_id=owner.telegram_user_id,
        subscription_id=int(subscription["id"]),
    )
    assert by_id is not None
    assert by_id["feed"]["site_url"] == "https://example.com"

    await repo.async_set_subscription_active(int(subscription["id"]), is_active=False)
    assert await repo.async_list_user_active_subscriptions(owner.telegram_user_id) == []


@pytest.mark.asyncio
async def test_rss_feed_repository_items_and_delivery_targets(database: Database) -> None:
    repo = RSSFeedRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=8201, username="rss-owner")
    other = await _create_user(database, telegram_user_id=8202, username="rss-other")
    inactive = await _create_user(database, telegram_user_id=8203, username="rss-inactive")
    feed = await repo.async_get_or_create_feed("https://example.com/items.xml")

    await repo.async_create_subscription(user_id=owner.telegram_user_id, feed_id=int(feed["id"]))
    await repo.async_create_subscription(user_id=other.telegram_user_id, feed_id=int(feed["id"]))
    inactive_sub = await repo.async_create_subscription(
        user_id=inactive.telegram_user_id,
        feed_id=int(feed["id"]),
    )
    await repo.async_set_subscription_active(int(inactive_sub["id"]), is_active=False)

    published_at = dt.datetime(2026, 5, 1, 12, tzinfo=UTC)
    older_item = await repo.async_create_feed_item(
        feed_id=int(feed["id"]),
        guid="guid-1",
        title="Post",
        url="https://example.com/post",
        content="content",
        author="Author",
        published_at=published_at,
    )
    newer_item = await repo.async_create_feed_item(
        feed_id=int(feed["id"]),
        guid="guid-2",
        title="Newer Post",
        url="https://example.com/newer-post",
        content="newer content",
        author="Author",
        published_at=published_at + dt.timedelta(hours=1),
    )
    assert older_item is not None
    assert newer_item is not None
    assert older_item["feed"] == feed["id"]
    assert older_item["published_at"] == published_at
    duplicate = await repo.async_create_feed_item(
        feed_id=int(feed["id"]),
        guid="guid-1",
        title="Post",
        url="https://example.com/post",
        content="content",
        author="Author",
        published_at=published_at,
    )
    assert duplicate is None
    bulk_items = await repo.async_create_feed_items(
        int(feed["id"]),
        [
            {
                "guid": "guid-1",
                "title": "Duplicate Post",
                "url": "https://example.com/duplicate-post",
                "content": "duplicate content",
                "author": "Author",
                "published_at": published_at + dt.timedelta(hours=2),
            },
            {
                "guid": "guid-3",
                "title": "Bulk Post",
                "url": "https://example.com/bulk-post",
                "content": "bulk content",
                "author": "Author",
                "published_at": published_at - dt.timedelta(hours=1),
            },
            {
                "guid": "guid-4",
                "title": "Second Bulk Post",
                "url": "https://example.com/second-bulk-post",
                "content": "second bulk content",
                "author": "Author",
                "published_at": published_at - dt.timedelta(hours=2),
            },
        ],
    )
    assert [item["guid"] for item in bulk_items] == ["guid-3", "guid-4"]

    items = await repo.async_list_feed_items(int(feed["id"]))
    assert [row["guid"] for row in items] == ["guid-2", "guid-1", "guid-3", "guid-4"]

    targets = await repo.async_list_delivery_targets([int(older_item["id"]), int(newer_item["id"])])
    assert [target["guid"] for target in targets] == ["guid-2", "guid-1"]
    assert [target["subscriber_ids"] for target in targets] == [
        [owner.telegram_user_id, other.telegram_user_id],
        [owner.telegram_user_id, other.telegram_user_id],
    ]

    await repo.async_mark_item_delivered(
        user_id=owner.telegram_user_id, item_id=int(older_item["id"])
    )
    await repo.async_mark_item_delivered(
        user_id=owner.telegram_user_id, item_id=int(older_item["id"])
    )
    await repo.async_mark_item_delivered(
        user_id=other.telegram_user_id, item_id=int(newer_item["id"])
    )
    await repo.async_mark_items_delivered(
        [
            (owner.telegram_user_id, int(newer_item["id"])),
            (owner.telegram_user_id, int(newer_item["id"])),
            (other.telegram_user_id, int(older_item["id"])),
        ]
    )

    remaining = await repo.async_list_delivery_targets(
        [int(older_item["id"]), int(newer_item["id"])]
    )
    assert remaining == []

    async with database.session() as session:
        deliveries = list(await session.scalars(select(RSSItemDelivery)))
    assert len(deliveries) == 4


@pytest.mark.asyncio
async def test_rss_feed_repository_active_feeds_limit_and_ordering(database: Database) -> None:
    repo = RSSFeedRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=8301, username="rss-limit")

    base = dt.datetime(2026, 6, 1, tzinfo=UTC)
    # Each feed gets an active subscription; distinct last_fetched_at values (one
    # NULL) drive the least-recently-fetched-first ordering.
    specs = [
        ("https://example.com/a.xml", base + dt.timedelta(hours=4)),
        ("https://example.com/b.xml", base + dt.timedelta(hours=1)),
        ("https://example.com/never.xml", None),
        ("https://example.com/c.xml", base + dt.timedelta(hours=3)),
        ("https://example.com/d.xml", base + dt.timedelta(hours=2)),
    ]
    for url, fetched in specs:
        feed = await repo.async_get_or_create_feed(url)
        await repo.async_create_subscription(
            user_id=owner.telegram_user_id, feed_id=int(feed["id"])
        )
        if fetched is not None:
            await repo.async_update_feed(int(feed["id"]), last_fetched_at=fetched)

    expected_order = [
        "https://example.com/never.xml",  # never fetched -> nulls first
        "https://example.com/b.xml",  # oldest fetch
        "https://example.com/d.xml",
        "https://example.com/c.xml",
        "https://example.com/a.xml",  # most recent fetch
    ]

    # No limit -> every active feed, least-recently-fetched first.
    unbounded = await repo.async_list_active_feeds()
    assert [row["url"] for row in unbounded] == expected_order

    # A positive limit caps the batch to the first N in that stable order.
    limited = await repo.async_list_active_feeds(limit=3)
    assert [row["url"] for row in limited] == expected_order[:3]

    # A non-positive limit is treated as unbounded.
    assert len(await repo.async_list_active_feeds(limit=0)) == len(specs)


@pytest.mark.asyncio
async def test_rss_feed_repository_fetch_status_updates(database: Database) -> None:
    repo = RSSFeedRepositoryAdapter(database)
    feed = await repo.async_get_or_create_feed("https://example.com/status.xml")

    await repo.async_record_feed_fetch_error(
        feed_id=int(feed["id"]),
        error="x" * 600,
        max_fetch_errors=2,
    )
    first_error = await repo.async_get_feed(int(feed["id"]))
    assert first_error is not None
    assert first_error["fetch_error_count"] == 1
    assert len(first_error["last_error"]) == 500
    assert first_error["is_active"] is True

    await repo.async_record_feed_fetch_error(
        feed_id=int(feed["id"]),
        error="still failing",
        max_fetch_errors=2,
    )
    disabled = await repo.async_get_feed(int(feed["id"]))
    assert disabled is not None
    assert disabled["fetch_error_count"] == 2
    assert disabled["is_active"] is False

    await repo.async_update_feed_fetch_success(
        feed_id=int(feed["id"]),
        title="Recovered",
        description="OK",
        site_url="https://example.com",
        etag="etag",
        last_modified="Wed, 06 May 2026 00:00:00 GMT",
    )
    recovered = await repo.async_get_feed(int(feed["id"]))
    assert recovered is not None
    assert recovered["title"] == "Recovered"
    assert recovered["fetch_error_count"] == 0
    assert recovered["last_error"] is None
    assert recovered["last_successful_at"] is not None
