from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _create_subscription(
    *,
    user_id: int,
    username: str,
    title: str | None = None,
    category: ChannelCategory | None = None,
    is_active: bool = True,
) -> ChannelSubscription:
    channel = Channel.create(  # type: ignore[attr-defined]
        username=username,
        title=title or username.title(),
        is_active=True,
    )
    return ChannelSubscription.create(  # type: ignore[attr-defined]
        user=user_id,
        channel=channel,
        category=category,
        is_active=is_active,
    )


@pytest.mark.asyncio
async def test_resolve_channel_updates_channel_metadata_and_subscription_state(
    db,
    user_factory,
    digest_service: DigestAPIService,
    monkeypatch,
) -> None:
    user = user_factory(username="digest-resolve", telegram_user_id=8001)
    channel = Channel.create(  # type: ignore[attr-defined]
        username="resolvedchan",
        title="Old Title",
        description="Old description",
        member_count=10,
        is_active=True,
    )
    ChannelSubscription.create(user=user.telegram_user_id, channel=channel, is_active=True)  # type: ignore[attr-defined]

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

    refreshed = Channel.get(Channel.username == "resolvedchan")  # type: ignore[attr-defined]
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


def test_list_channel_posts_returns_paginated_posts_with_analysis(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = user_factory(username="digest-posts", telegram_user_id=8002)
    subscription = _create_subscription(user_id=user.telegram_user_id, username="postchan")
    newest = ChannelPost.create(  # type: ignore[attr-defined]
        channel=subscription.channel,
        message_id=101,
        text="A" * 700,
        date=datetime.now(UTC),
        views=120,
        forwards=7,
        media_type="photo",
        url="https://t.me/postchan/101",
    )
    ChannelPostAnalysis.create(  # type: ignore[attr-defined]
        post=newest,
        real_topic="Digest Topic",
        tldr="Concise summary",
        relevance_score=0.85,
        content_type="news",
    )
    ChannelPost.create(  # type: ignore[attr-defined]
        channel=subscription.channel,
        message_id=100,
        text="Older post",
        date=datetime.now(UTC) - timedelta(hours=1),
        url="https://t.me/postchan/100",
    )

    result = digest_service.list_channel_posts(
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


def test_list_channel_posts_validates_input_and_subscription(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = user_factory(username="digest-post-errors", telegram_user_id=8003)
    _create_subscription(user_id=user.telegram_user_id, username="allowedchan")
    Channel.create(username="orphaned", title="Orphaned", is_active=True)  # type: ignore[attr-defined]

    with pytest.raises(ValidationError, match="Invalid"):
        digest_service.list_channel_posts(user.telegram_user_id, "x")

    with pytest.raises(ValidationError, match="not found"):
        digest_service.list_channel_posts(user.telegram_user_id, "@missingchannel")

    with pytest.raises(ValidationError, match="Not subscribed"):
        digest_service.list_channel_posts(user.telegram_user_id, "@orphaned")


def test_update_preferences_updates_existing_records_and_validates_time_formats(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = user_factory(username="digest-prefs", telegram_user_id=8004)
    pref = UserDigestPreference.create(  # type: ignore[attr-defined]
        user=user.telegram_user_id,
        delivery_time="09:00",
        timezone=None,
        hours_lookback=24,
        max_posts_per_digest=10,
        min_relevance_score=0.3,
    )

    updated = digest_service.update_preferences(
        user.telegram_user_id,
        delivery_time="11:30",
        hours_lookback=12,
        max_posts_per_digest=7,
        min_relevance_score=0.75,
    )

    refreshed = UserDigestPreference.get(UserDigestPreference.user == user.telegram_user_id)  # type: ignore[attr-defined]
    assert updated.delivery_time == "11:30"
    assert updated.delivery_time_source == "user"
    assert updated.timezone == "UTC"
    assert updated.timezone_source == "global"
    assert updated.hours_lookback == 12
    assert updated.max_posts_per_digest == 7
    assert updated.min_relevance_score == 0.75
    assert refreshed.updated_at is not None

    with pytest.raises(ValidationError, match="valid integers"):
        digest_service.update_preferences(user.telegram_user_id, delivery_time="11:xx")

    with pytest.raises(ValidationError, match="Invalid hour/minute"):
        digest_service.update_preferences(user.telegram_user_id, delivery_time="24:00")


def test_category_crud_and_assignment_flows(
    db, user_factory, digest_service: DigestAPIService
) -> None:
    user = user_factory(username="digest-cats", telegram_user_id=8005)
    first = digest_service.create_category(user.telegram_user_id, "News")
    second = digest_service.create_category(user.telegram_user_id, "Tech")
    subscription = _create_subscription(
        user_id=user.telegram_user_id,
        username="catchan",
        category=ChannelCategory.get_by_id(first.id),  # type: ignore[attr-defined]
    )

    listed = digest_service.list_categories(user.telegram_user_id)
    assert [item.name for item in listed] == ["News", "Tech"]
    assert listed[0].subscription_count == 1

    renamed = digest_service.update_category(
        user.telegram_user_id,
        first.id,
        name="World News",
        position=5,
    )
    assert renamed.name == "World News"
    assert renamed.position == 5

    subscriptions = digest_service.list_subscriptions(user.telegram_user_id)
    assert subscriptions["channels"][0].category_name == "World News"

    assert digest_service.assign_category(user.telegram_user_id, subscription.id, second.id) == {
        "status": "updated"
    }
    assert digest_service.assign_category(user.telegram_user_id, subscription.id, None) == {
        "status": "updated"
    }
    assert digest_service.delete_category(user.telegram_user_id, second.id) == {"status": "deleted"}

    with pytest.raises(ValidationError, match="already exists"):
        digest_service.create_category(user.telegram_user_id, "World News")

    with pytest.raises(ValidationError, match="Category not found"):
        digest_service.update_category(user.telegram_user_id, 999999, name="Missing")

    with pytest.raises(ValidationError, match="Category not found"):
        digest_service.delete_category(user.telegram_user_id, 999999)

    with pytest.raises(ValidationError, match="Subscription not found"):
        digest_service.assign_category(user.telegram_user_id, 999999, None)

    with pytest.raises(ValidationError, match="Category not found"):
        digest_service.assign_category(user.telegram_user_id, subscription.id, 999999)


def test_digest_category_assignment_rejects_cross_user_ids(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    owner = user_factory(username="digest-owner", telegram_user_id=8015)
    other = user_factory(username="digest-other", telegram_user_id=8016)
    owner_category = digest_service.create_category(owner.telegram_user_id, "Owner")
    other_category = digest_service.create_category(other.telegram_user_id, "Other")
    owner_subscription = _create_subscription(
        user_id=owner.telegram_user_id,
        username="ownerchan",
        category=ChannelCategory.get_by_id(owner_category.id),  # type: ignore[attr-defined]
    )
    other_subscription = _create_subscription(
        user_id=other.telegram_user_id,
        username="otherchan",
        category=ChannelCategory.get_by_id(other_category.id),  # type: ignore[attr-defined]
    )

    with pytest.raises(ValidationError, match="Subscription not found"):
        digest_service.assign_category(
            owner.telegram_user_id,
            other_subscription.id,
            owner_category.id,
        )

    with pytest.raises(ValidationError, match="Category not found"):
        digest_service.assign_category(
            owner.telegram_user_id,
            owner_subscription.id,
            other_category.id,
        )


def test_bulk_digest_operations_report_mixed_results(
    db,
    user_factory,
    digest_service: DigestAPIService,
) -> None:
    user = user_factory(username="digest-bulk", telegram_user_id=8006)
    sub_one = _create_subscription(user_id=user.telegram_user_id, username="bulkone")
    sub_two = _create_subscription(user_id=user.telegram_user_id, username="bulktwo")
    Channel.create(username="orphanbulk", title="Orphan", is_active=True)  # type: ignore[attr-defined]
    category = ChannelCategory.create(user=user.telegram_user_id, name="Bulk", position=1)  # type: ignore[attr-defined]

    unsubscribe_result = digest_service.bulk_unsubscribe(
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

    assign_result = digest_service.bulk_assign_category(
        user.telegram_user_id,
        [sub_one.id, sub_two.id, 999999],
        category.id,
    )
    assert assign_result["success_count"] == 2
    assert assign_result["error_count"] == 1
    assert {"id": str(sub_one.id), "status": "updated"} in assign_result["results"]
    assert {"id": "999999", "status": "error", "detail": "not_found"} in assign_result["results"]

    with pytest.raises(ValidationError, match="Category not found"):
        digest_service.bulk_assign_category(user.telegram_user_id, [sub_one.id], 424242)
