"""Digest channel posts mirrored into generic signal source tables."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.time_utils import UTC
from app.db.models import (
    Channel,
    ChannelPost,
    ChannelSubscription,
    FeedItem,
    Source,
    Subscription,
    User,
)
from app.infrastructure.persistence.digest_store import DigestStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


async def test_digest_store_mirrors_channel_posts_to_signal_tables(
    database: Database, session: AsyncSession
) -> None:
    async with database.transaction() as s:
        s.add(User(telegram_user_id=1001, username="owner"))
        await s.flush()
        channel = Channel(username="python_daily", title="Python Daily", channel_id=123)
        s.add(channel)
        await s.flush()
        s.add(ChannelSubscription(user_id=1001, channel_id=channel.id))
        await s.flush()
        channel_id = channel.id

    posts = [
        {
            "message_id": 42,
            "text": "Python release notes",
            "date": dt.datetime(2026, 4, 30, tzinfo=UTC),
            "views": 100,
            "forwards": 3,
            "url": "https://t.me/python_daily/42",
            "media_type": "text",
        }
    ]

    async with database.session() as s:
        channel = await s.get(Channel, channel_id)

    store = DigestStore(database=database)
    await store.async_persist_posts(channel, posts)
    await store.async_mirror_posts_to_signal_sources(user_id=1001, channel=channel, posts=posts)

    async with database.session() as s:
        source = await s.scalar(
            select(Source).where(
                Source.kind == "telegram_channel", Source.external_id == "python_daily"
            )
        )
        assert source is not None
        item = await s.scalar(
            select(FeedItem).where(FeedItem.source_id == source.id, FeedItem.external_id == "42")
        )
        assert item is not None
        subscription = await s.scalar(
            select(Subscription).where(
                Subscription.source_id == source.id, Subscription.user_id == 1001
            )
        )
        assert subscription is not None

    assert item.content_text == "Python release notes"
    assert item.views == 100
    assert item.forwards == 3
    assert subscription.is_active is True


async def test_digest_store_bulk_persist_and_mirror_handles_existing_and_duplicate_posts(
    database: Database, session: AsyncSession
) -> None:
    async with database.transaction() as s:
        s.add(User(telegram_user_id=1002, username="bulk-owner"))
        await s.flush()
        channel = Channel(username="python_bulk", title="Python Bulk", channel_id=456)
        s.add(channel)
        await s.flush()
        channel_id = channel.id

    first_published_at = dt.datetime(2026, 5, 1, tzinfo=UTC)
    second_published_at = dt.datetime(2026, 5, 2, tzinfo=UTC)
    posts = [
        {
            "message_id": 42,
            "text": "Original post",
            "date": first_published_at,
            "views": 10,
            "forwards": 1,
            "url": "https://t.me/python_bulk/42",
            "media_type": "text",
        },
        {
            "message_id": 43,
            "text": "First duplicate version",
            "date": first_published_at,
            "views": 20,
            "forwards": 2,
            "url": "https://t.me/python_bulk/43",
            "media_type": "text",
        },
        {
            "message_id": 43,
            "text": "Second duplicate version",
            "date": second_published_at,
            "views": 30,
            "forwards": 3,
            "url": "https://t.me/python_bulk/43",
            "media_type": "photo",
        },
    ]

    async with database.session() as s:
        channel = await s.get(Channel, channel_id)

    store = DigestStore(database=database)
    await store.async_persist_posts(channel, posts)
    await store.async_persist_posts(channel, posts)

    async with database.session() as s:
        post_count = await s.scalar(
            select(func.count(ChannelPost.id)).where(ChannelPost.channel_id == channel_id)
        )
        assert post_count == 2

    await store.async_mirror_posts_to_signal_sources(user_id=1002, channel=channel, posts=posts)
    await store.async_mirror_posts_to_signal_sources(user_id=1002, channel=channel, posts=posts)

    async with database.session() as s:
        source = await s.scalar(
            select(Source).where(
                Source.kind == "telegram_channel", Source.external_id == "python_bulk"
            )
        )
        assert source is not None
        item_count = await s.scalar(
            select(func.count(FeedItem.id)).where(FeedItem.source_id == source.id)
        )
        assert item_count == 2
        duplicate_item = await s.scalar(
            select(FeedItem).where(FeedItem.source_id == source.id, FeedItem.external_id == "43")
        )
        assert duplicate_item is not None
        channel_post = await s.scalar(
            select(ChannelPost).where(
                ChannelPost.channel_id == channel_id,
                ChannelPost.message_id == 43,
            )
        )
        assert channel_post is not None

    assert duplicate_item.content_text == "Second duplicate version"
    assert duplicate_item.views == 30
    assert duplicate_item.forwards == 3
    assert duplicate_item.metadata_json == {"media_type": "photo"}
    assert duplicate_item.legacy_channel_post_id == channel_post.id
