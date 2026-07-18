from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.api.exceptions import ValidationError
from app.api.services.digest_api_service import DigestAPIService
from app.config.digest import ChannelDigestConfig
from app.core.time_utils import UTC
from app.db.models import (
    Channel,
    ChannelCategory,
    ChannelPost,
    ChannelPostAnalysis,
    ChannelSubscription,
    UserDigestPreference,
)


@pytest.fixture
def digest_service() -> DigestAPIService:
    return DigestAPIService(ChannelDigestConfig(enabled=True))


async def _create_subscription(
    db,
    *,
    user_id: int,
    username: str,
    title: str | None = None,
    category_id: int | None = None,
    is_active: bool = True,
) -> ChannelSubscription:
    async with db.transaction() as session:
        channel = Channel(
            username=username,
            title=title or username.title(),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        subscription = ChannelSubscription(
            user_id=user_id,
            channel_id=channel.id,
            category_id=category_id,
            is_active=is_active,
        )
        session.add(subscription)
        await session.flush()
        return subscription


@pytest.mark.asyncio
async def test_resolve_channel_updates_channel_metadata_and_subscription_state(
    db,
    user_factory,
    digest_service: DigestAPIService,
    monkeypatch,
) -> None:
    user = await user_factory(username="digest-resolve", telegram_user_id=8001)
    await _create_subscription(
        db,
        user_id=user.telegram_user_id,
        username="resolvedchan",
        title="Old Title",
    )
    async with db.transaction() as session:
        channel = await session.scalar(select(Channel).where(Channel.username == "resolvedchan"))
        assert channel is not None
        channel.description = "Old description"
        channel.member_count = 10

    userbot = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        resolve_channel=AsyncMock(
            return_value={
                "username": "resolvedchan",
                "title": "New Title",
                "description": "Fresh description",
                "member_count": 1234,
            }
        ),
    )
    monkeypatch.setattr("app.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "app.adapters.digest.userbot_client.UserbotClient",
        MagicMock(return_value=userbot),
    )

    resolved = await digest_service.resolve_channel(user.telegram_user_id, "@ResolvedChan")

    async with db.session() as session:
        refreshed = await session.scalar(select(Channel).where(Channel.username == "resolvedchan"))
    assert refreshed is not None
    assert resolved.username == "resolvedchan"
    assert resolved.title == "New Title"
    assert resolved.description == "Fresh description"
    assert resolved.member_count == 1234
    assert resolved.is_subscribed is True
    assert refreshed.title == "New Title"
    assert refreshed.description == "Fresh description"
    assert refreshed.member_count == 1234
    userbot.start.assert_awaited_once()
    userbot.stop.assert_awaited_once()


async def test_list_channel_posts_returns_paginated_posts_with_analysis(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = await user_factory(username="digest-posts", telegram_user_id=8002)
    subscription = await _create_subscription(
        db, user_id=user.telegram_user_id, username="postchan"
    )
    async with db.transaction() as session:
        newest = ChannelPost(
            channel_id=subscription.channel_id,
            message_id=101,
            text="A" * 700,
            date=datetime.now(UTC),
            views=120,
            forwards=7,
            media_type="photo",
            url="https://t.me/postchan/101",
        )
        session.add(newest)
        await session.flush()
        session.add(
            ChannelPostAnalysis(
                post_id=newest.id,
                real_topic="Digest Topic",
                tldr="Concise summary",
                relevance_score=0.85,
                content_type="news",
            )
        )
        session.add(
            ChannelPost(
                channel_id=subscription.channel_id,
                message_id=100,
                text="Older post",
                date=datetime.now(UTC) - timedelta(hours=1),
                url="https://t.me/postchan/100",
            )
        )

    result = await asyncio.to_thread(
        digest_service.list_channel_posts,
        user.telegram_user_id,
        "@postchan",
        limit=1,
        offset=0,
    )

    assert result["total"] == 2
    assert result["channel_username"] == "postchan"
    assert len(result["posts"]) == 1
    post = result["posts"][0]
    assert post.message_id == 101
    assert len(post.text) == 500
    assert post.analysis is not None
    assert post.analysis.real_topic == "Digest Topic"
    assert post.analysis.relevance_score == 0.85


async def test_list_channel_posts_validates_input_and_subscription(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = await user_factory(username="digest-post-errors", telegram_user_id=8003)
    await _create_subscription(db, user_id=user.telegram_user_id, username="allowedchan")
    async with db.transaction() as session:
        session.add(Channel(username="orphaned", title="Orphaned", is_active=True))

    with pytest.raises(ValidationError, match="Invalid"):
        await asyncio.to_thread(digest_service.list_channel_posts, user.telegram_user_id, "x")

    with pytest.raises(ValidationError, match="not found"):
        await asyncio.to_thread(
            digest_service.list_channel_posts,
            user.telegram_user_id,
            "@missingchannel",
        )

    with pytest.raises(ValidationError, match="Not subscribed"):
        await asyncio.to_thread(
            digest_service.list_channel_posts,
            user.telegram_user_id,
            "@orphaned",
        )


async def test_update_preferences_updates_existing_records_and_validates_time_formats(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = await user_factory(username="digest-prefs", telegram_user_id=8004)
    async with db.transaction() as session:
        session.add(
            UserDigestPreference(
                user_id=user.telegram_user_id,
                delivery_time="09:00",
                timezone=None,
                hours_lookback=24,
                max_posts_per_digest=10,
                min_relevance_score=0.3,
            )
        )

    updated = await asyncio.to_thread(
        digest_service.update_preferences,
        user.telegram_user_id,
        delivery_time="11:30",
        hours_lookback=12,
        max_posts_per_digest=7,
        min_relevance_score=0.75,
    )

    async with db.session() as session:
        refreshed = await session.scalar(
            select(UserDigestPreference).where(
                UserDigestPreference.user_id == user.telegram_user_id
            )
        )
    assert refreshed is not None
    assert updated.delivery_time == "11:30"
    assert updated.delivery_time_source == "user"
    assert updated.timezone == "UTC"
    assert updated.timezone_source == "global"
    assert updated.hours_lookback == 12
    assert updated.max_posts_per_digest == 7
    assert updated.min_relevance_score == 0.75
    assert refreshed.updated_at is not None

    with pytest.raises(ValidationError, match="valid integers"):
        await asyncio.to_thread(
            digest_service.update_preferences,
            user.telegram_user_id,
            delivery_time="11:xx",
        )

    with pytest.raises(ValidationError, match="Invalid hour/minute"):
        await asyncio.to_thread(
            digest_service.update_preferences,
            user.telegram_user_id,
            delivery_time="24:00",
        )


async def test_category_crud_and_assignment_flows(
    db, user_factory, digest_service: DigestAPIService
) -> None:
    user = await user_factory(username="digest-cats", telegram_user_id=8005)
    first = await asyncio.to_thread(digest_service.create_category, user.telegram_user_id, "News")
    second = await asyncio.to_thread(digest_service.create_category, user.telegram_user_id, "Tech")
    subscription = await _create_subscription(
        db,
        user_id=user.telegram_user_id,
        username="catchan",
        category_id=first.id,
    )

    listed = await asyncio.to_thread(digest_service.list_categories, user.telegram_user_id)
    assert [item.name for item in listed] == ["News", "Tech"]
    assert listed[0].subscription_count == 1

    renamed = await asyncio.to_thread(
        digest_service.update_category,
        user.telegram_user_id,
        first.id,
        name="World News",
        position=5,
    )
    assert renamed.name == "World News"
    assert renamed.position == 5

    subscriptions = await asyncio.to_thread(
        digest_service.list_subscriptions, user.telegram_user_id
    )
    assert subscriptions["channels"][0].category_name == "World News"

    assert await asyncio.to_thread(
        digest_service.assign_category,
        user.telegram_user_id,
        subscription.id,
        second.id,
    ) == {"status": "updated"}
    assert await asyncio.to_thread(
        digest_service.assign_category,
        user.telegram_user_id,
        subscription.id,
        None,
    ) == {"status": "updated"}
    assert await asyncio.to_thread(
        digest_service.delete_category, user.telegram_user_id, second.id
    ) == {"status": "deleted"}

    with pytest.raises(ValidationError, match="already exists"):
        await asyncio.to_thread(digest_service.create_category, user.telegram_user_id, "World News")

    with pytest.raises(ValidationError, match="Category not found"):
        await asyncio.to_thread(
            digest_service.update_category,
            user.telegram_user_id,
            999999,
            name="Missing",
        )

    with pytest.raises(ValidationError, match="Category not found"):
        await asyncio.to_thread(digest_service.delete_category, user.telegram_user_id, 999999)

    with pytest.raises(ValidationError, match="Subscription not found"):
        await asyncio.to_thread(
            digest_service.assign_category,
            user.telegram_user_id,
            999999,
            None,
        )

    with pytest.raises(ValidationError, match="Category not found"):
        await asyncio.to_thread(
            digest_service.assign_category,
            user.telegram_user_id,
            subscription.id,
            999999,
        )


async def test_digest_category_assignment_rejects_cross_user_ids(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    owner = await user_factory(username="digest-owner", telegram_user_id=8015)
    other = await user_factory(username="digest-other", telegram_user_id=8016)
    owner_category = await asyncio.to_thread(
        digest_service.create_category, owner.telegram_user_id, "Owner"
    )
    other_category = await asyncio.to_thread(
        digest_service.create_category, other.telegram_user_id, "Other"
    )
    owner_subscription = await _create_subscription(
        db,
        user_id=owner.telegram_user_id,
        username="ownerchan",
        category_id=owner_category.id,
    )
    other_subscription = await _create_subscription(
        db,
        user_id=other.telegram_user_id,
        username="otherchan",
        category_id=other_category.id,
    )

    with pytest.raises(ValidationError, match="Subscription not found"):
        await asyncio.to_thread(
            digest_service.assign_category,
            owner.telegram_user_id,
            other_subscription.id,
            owner_category.id,
        )

    with pytest.raises(ValidationError, match="Category not found"):
        await asyncio.to_thread(
            digest_service.assign_category,
            owner.telegram_user_id,
            owner_subscription.id,
            other_category.id,
        )


async def test_bulk_digest_operations_report_mixed_results(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = await user_factory(username="digest-bulk", telegram_user_id=8006)
    sub_one = await _create_subscription(db, user_id=user.telegram_user_id, username="bulkone")
    sub_two = await _create_subscription(db, user_id=user.telegram_user_id, username="bulktwo")
    async with db.transaction() as session:
        session.add(Channel(username="orphanbulk", title="Orphan", is_active=True))
        category = ChannelCategory(user_id=user.telegram_user_id, name="Bulk", position=1)
        session.add(category)
        await session.flush()

    unsubscribe_result = await asyncio.to_thread(
        digest_service.bulk_unsubscribe,
        user.telegram_user_id,
        ["bulkone", "orphanbulk", "missingbulk", "x"],
    )

    assert unsubscribe_result["success_count"] == 1
    assert unsubscribe_result["error_count"] == 3
    assert {"username": "bulkone", "status": "unsubscribed"} in unsubscribe_result["results"]
    assert {"username": "orphanbulk", "status": "error", "detail": "not_subscribed"} in (
        unsubscribe_result["results"]
    )
    assert {"username": "missingbulk", "status": "error", "detail": "not_found"} in (
        unsubscribe_result["results"]
    )

    assign_result = await asyncio.to_thread(
        digest_service.bulk_assign_category,
        user.telegram_user_id,
        [sub_one.id, sub_two.id, 999999],
        category.id,
    )
    assert assign_result["success_count"] == 2
    assert assign_result["error_count"] == 1
    assert {"id": str(sub_one.id), "status": "updated"} in assign_result["results"]
    assert {"id": "999999", "status": "error", "detail": "not_found"} in assign_result["results"]

    with pytest.raises(ValidationError, match="Category not found"):
        await asyncio.to_thread(
            digest_service.bulk_assign_category,
            user.telegram_user_id,
            [sub_one.id],
            424242,
        )
