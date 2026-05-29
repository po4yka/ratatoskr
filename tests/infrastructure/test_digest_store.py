from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.core.time_utils import UTC
from app.db.models import (
    Channel,
    ChannelCategory,
    ChannelPost,
    ChannelPostAnalysis,
    ChannelSubscription,
    DigestDelivery,
    FeedItem,
    Source,
    Subscription,
    UserDigestPreference,
)
from app.infrastructure.persistence import digest_store as digest_store_module
from app.infrastructure.persistence.digest_store import DigestStore


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _ExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return _Rows(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _DigestSession:
    def __init__(
        self,
        *,
        scalar_values: list[Any] | None = None,
        execute_rows: list[list[Any]] | None = None,
        get_values: list[Any] | None = None,
    ) -> None:
        self.scalar_values = list(scalar_values or [])
        self.execute_rows = list(execute_rows or [])
        self.get_values = list(get_values or [])
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.merged: list[Any] = []
        self.executed: list[Any] = []
        self.flush_count = 0

    async def scalar(self, statement: Any) -> Any:
        self.executed.append(statement)
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def execute(self, statement: Any) -> _ExecuteResult:
        self.executed.append(statement)
        rows = self.execute_rows.pop(0) if self.execute_rows else []
        return _ExecuteResult(rows)

    async def get(self, model: Any, key: Any) -> Any:
        return self.get_values.pop(0) if self.get_values else None

    def add(self, instance: Any) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = 1000 + len(self.added)
        self.added.append(instance)

    async def flush(self) -> None:
        self.flush_count += 1

    async def merge(self, instance: Any) -> Any:
        self.merged.append(instance)
        return instance

    async def delete(self, instance: Any) -> None:
        self.deleted.append(instance)


class _DigestDb:
    def __init__(
        self,
        *,
        scalar_values: list[Any] | None = None,
        execute_rows: list[list[Any]] | None = None,
        get_values: list[Any] | None = None,
    ) -> None:
        self.session_obj = _DigestSession(
            scalar_values=scalar_values,
            execute_rows=execute_rows,
            get_values=get_values,
        )

    @asynccontextmanager
    async def session(self):
        yield self.session_obj

    @asynccontextmanager
    async def transaction(self):
        yield self.session_obj


@pytest.mark.asyncio
async def test_digest_store_reads_counts_and_basic_crud() -> None:
    channel = Channel(id=10, username="updates", title="Updates", is_active=True)
    category = ChannelCategory(id=20, user_id=1, name="Tech", position=2)
    subscription = ChannelSubscription(
        id=30,
        user_id=1,
        channel_id=10,
        category_id=20,
        is_active=True,
    )
    subscription.channel = channel
    subscription.category = category
    preference = UserDigestPreference(id=40, user_id=1, delivery_time="09:00")
    post = ChannelPost(
        id=50,
        channel_id=10,
        message_id=101,
        text="post",
        date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    analysis = ChannelPostAnalysis(
        id=60,
        post_id=50,
        real_topic="AI",
        tldr="Short",
        key_insights=["one"],
        relevance_score=0.9,
        content_type="news",
    )
    delivery = DigestDelivery(
        id=70,
        user_id=1,
        digest_type="daily",
        posts_json=[101],
        delivered_at=datetime(2024, 1, 2, tzinfo=UTC),
    )
    db = _DigestDb(
        scalar_values=[
            2,
            None,
            category,
            4,
            subscription,
            channel,
            10,
            channel,
            3,
            analysis,
            1,
            preference,
            preference,
            post,
        ],
        execute_rows=[
            [subscription],
            [category],
            [subscription],
            [post],
            [delivery],
            [subscription],
            [[101, "102"], None],
            [1],
        ],
    )
    store = DigestStore(db)

    assert await store.async_list_active_subscriptions(1) == [subscription]
    assert await store.async_count_active_subscriptions(1) == 2
    assert await store.async_count_active_subscriptions_for_category(category) == 0
    assert await store.async_get_category_for_user(1, 20) is category
    assert await store.async_list_categories(1) == [category]
    assert await store.async_next_category_position(1) == 5

    created = await store.async_create_category(user_id=1, name="New", position=3)
    assert created.name == "New"
    assert db.session_obj.flush_count == 1

    await store.async_save_model(category)
    await store.async_delete_model(category)
    assert db.session_obj.merged[-1] is category
    assert db.session_obj.deleted[-1] is category

    assert (
        await store.async_get_subscription_for_user(user_id=1, subscription_id=30) is subscription
    )
    assert await store.async_list_category_subscriptions(user_id=1, subscription_ids=[30]) == [
        subscription
    ]
    assert await store.async_get_or_create_channel("updates") is channel
    assert await store.async_is_user_subscribed(user_id=1, channel=channel) is True
    assert await store.async_get_channel_by_username("updates") is channel
    assert await store.async_count_channel_posts(channel) == 3
    assert await store.async_list_channel_posts(channel, limit=5, offset=0) == [post]
    assert await store.async_get_post_analysis(post) is analysis
    assert await store.async_list_deliveries(user_id=1, limit=10, offset=0) == [delivery]
    assert await store.async_count_deliveries(1) == 1
    assert await store.async_get_user_preference(1) is preference
    assert await store.async_get_or_create_user_preference(1, {}) == (preference, False)
    assert await store.async_list_active_feed_subscriptions_with_channels(1) == [subscription]
    assert await store.async_list_delivered_message_ids(1) == {101, 102}
    assert await store.async_get_channel_post(channel_id=10, message_id=101) is post
    assert await store.async_get_users_with_subscriptions() == [1]


@pytest.mark.asyncio
async def test_digest_store_creates_missing_rows_and_persists_posts() -> None:
    channel = Channel(
        id=10,
        username="updates",
        title="Updates",
        channel_id=123,
        description="desc",
        member_count=42,
        is_active=True,
        fetch_error_count=0,
    )
    db = _DigestDb(
        scalar_values=[None, None, None],
        execute_rows=[[101]],
        get_values=[channel],
    )
    store = DigestStore(db)

    created_channel = await store.async_get_or_create_channel("new", title="New Channel")
    assert created_channel.username == "new"
    assert created_channel.title == "New Channel"

    preference, created = await store.async_get_or_create_user_preference(
        1,
        {"delivery_time": "09:00", "timezone": "UTC"},
    )
    assert created is True
    assert preference.delivery_time == "09:00"

    await store.async_persist_posts(
        channel,
        [
            {
                "message_id": 101,
                "text": "duplicate",
                "date": datetime(2024, 1, 1, tzinfo=UTC),
            },
            {
                "message_id": 102,
                "text": "new",
                "date": datetime(2024, 1, 2, tzinfo=UTC),
                "media_type": "photo",
                "views": 10,
                "forwards": 2,
                "url": "https://t.me/updates/102",
            },
        ],
    )

    added_types = [type(instance).__name__ for instance in db.session_obj.added]
    assert added_types == ["Channel", "UserDigestPreference", "ChannelPost"]
    assert db.session_obj.added[-1].message_id == 102


@pytest.mark.asyncio
async def test_digest_store_mirrors_signal_sources_and_updates_controls() -> None:
    channel = Channel(
        id=10,
        username="updates",
        title="Updates",
        channel_id=123,
        description="desc",
        member_count=42,
        is_active=True,
        fetch_error_count=0,
    )
    source = Source(
        id=20,
        kind="telegram_channel",
        external_id="updates",
        metadata_json={"controls": {"max_items_per_run": "7"}},
        is_active=True,
    )
    subscription = Subscription(
        id=30,
        user_id=1,
        source_id=20,
        is_active=True,
        cadence_seconds=300,
    )
    channel_subscription = ChannelSubscription(
        id=40,
        user_id=1,
        channel_id=10,
        is_active=True,
    )
    existing_post = ChannelPost(
        id=50,
        channel_id=10,
        message_id=101,
        text="existing",
        date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    existing_item = FeedItem(id=60, source_id=20, external_id="101")
    db = _DigestDb(
        scalar_values=[
            source,
            subscription,
            source,
            subscription,
            subscription,
            channel_subscription,
            source,
            subscription,
            subscription,
            channel_subscription,
            source,
            subscription,
            subscription,
        ],
        execute_rows=[
            [existing_post],
            [existing_item],
        ],
        get_values=[channel, channel, channel],
    )
    store = DigestStore(db)

    await store.async_mirror_posts_to_signal_sources(
        user_id=1,
        channel=channel,
        posts=[
            {
                "message_id": 101,
                "text": "existing text",
                "date": datetime(2024, 1, 1, tzinfo=UTC),
                "url": "https://t.me/updates/101",
                "media_type": "text",
                "views": 5,
                "forwards": 1,
            },
            {
                "message_id": 102,
                "text": "new text",
                "date": datetime(2024, 1, 2, tzinfo=UTC),
            },
        ],
    )
    assert existing_item.canonical_url == "https://t.me/updates/101"
    assert existing_item.legacy_channel_post_id == 50
    assert [type(instance).__name__ for instance in db.session_obj.added] == ["FeedItem"]

    source.metadata_json = {"controls": {"max_items_per_run": "7"}}
    run_state = await store.async_get_channel_run_state(user_id=1, channel=channel)
    assert run_state["is_active"] is True
    assert run_state["active_subscription"] is True
    assert run_state["fetch_interval_seconds"] == 300
    assert run_state["max_items_per_run"] == 7

    updated = await store.async_update_channel_controls(
        user_id=1,
        username="updates",
        is_active=False,
        fetch_interval_seconds=600,
        max_items_per_run=3,
        retry_policy={"max_errors": 2},
    )
    assert updated is True
    assert channel.is_active is False
    assert subscription.is_active is False
    assert source.metadata_json["controls"] == {
        "max_items_per_run": 3,
        "retry_policy": {"max_errors": 2},
        "fetch_interval_seconds": 600,
    }

    retry_db = _DigestDb(
        scalar_values=[channel_subscription, source, subscription, subscription],
        get_values=[channel],
    )
    retried = await DigestStore(retry_db).async_retry_channel(user_id=1, username="updates")
    assert retried is True
    assert channel.is_active is True
    assert subscription.next_fetch_at is None


@pytest.mark.asyncio
async def test_digest_store_fetch_errors_and_analysis_paths() -> None:
    channel = Channel(
        id=10,
        username="updates",
        title="Updates",
        is_active=True,
        fetch_error_count=1,
    )
    source = Source(id=20, kind="telegram_channel", external_id="updates", is_active=True)
    analyzed_post = ChannelPost(
        id=30,
        channel_id=10,
        message_id=101,
        text="post",
        date=datetime(2024, 1, 1, tzinfo=UTC),
        analyzed_at=datetime(2024, 1, 2, tzinfo=UTC),
    )
    unanalyzed_post = ChannelPost(
        id=31,
        channel_id=10,
        message_id=102,
        text="post",
        date=datetime(2024, 1, 2, tzinfo=UTC),
    )
    analysis = ChannelPostAnalysis(
        id=40,
        post_id=30,
        real_topic="AI",
        tldr="Short",
        key_insights=["one"],
        relevance_score=0.8,
        content_type="news",
    )
    db = _DigestDb(
        # record_channel_fetch_error: execute(UPDATE channel) + scalar(source) +
        #   execute(UPDATE subscription);
        # find_cached_analysis: a single execute(JOIN).first() -> (post, analysis);
        # persist_analysis: scalar(ChannelPost) + scalar(existing=None).
        scalar_values=[source, unanalyzed_post, None],
        execute_rows=[[], [], [(analyzed_post, analysis)]],
    )
    store = DigestStore(db)

    assert await store.async_record_channel_fetch_error(channel, "timeout", max_errors=2) is True
    assert source.is_active is False
    assert source.last_error == "timeout"

    cached = await store.async_find_cached_analysis(
        {"_channel_id": 10, "message_id": 101, "title": "existing"}
    )
    assert cached == {
        "_channel_id": 10,
        "message_id": 101,
        "title": "existing",
        "real_topic": "AI",
        "tldr": "Short",
        "key_insights": ["one"],
        "relevance_score": 0.8,
        "content_type": "news",
        "is_ad": False,
    }

    await store.async_persist_analysis(
        {"_channel_id": 10, "message_id": 102},
        {
            "real_topic": "Infra",
            "tldr": "Saved",
            "key_insights": ["two"],
            "relevance_score": 0.7,
            "content_type": "analysis",
        },
    )
    assert type(db.session_obj.added[-1]).__name__ == "ChannelPostAnalysis"
    assert db.session_obj.added[-1].post_id == 31

    await store.async_create_delivery(
        user_id=1,
        post_count=2,
        channel_count=1,
        digest_type="daily",
        correlation_id="corr-1",
        post_ids=[101, 102],
    )
    assert type(db.session_obj.added[-1]).__name__ == "DigestDelivery"
    assert db.session_obj.added[-1].posts_json == [101, 102]


@pytest.mark.asyncio
async def test_digest_store_fetchable_and_due_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    due_subscription = ChannelSubscription(id=1, user_id=1, channel_id=10, is_active=True)
    due_subscription.channel = Channel(id=10, username="due", is_active=True)
    inactive_subscription = ChannelSubscription(id=2, user_id=1, channel_id=11, is_active=True)
    inactive_subscription.channel = Channel(id=11, username="inactive", is_active=True)

    async def fake_active_subscriptions(user_id: int) -> list[ChannelSubscription]:
        return [due_subscription, inactive_subscription]

    run_states_by_channel = {
        10: {
            "is_active": True,
            "active_subscription": True,
            "backoff_until": now - timedelta(seconds=1),
        },
        11: {"is_active": False, "active_subscription": True, "backoff_until": None},
    }

    async def fake_batch_run_states(
        session: Any, *, user_id: int, channels: list[Any]
    ) -> dict[int, dict[str, Any]]:
        return {channel.id: run_states_by_channel[channel.id] for channel in channels}

    monkeypatch.setattr(digest_store_module, "utc_now", lambda: now)
    store = DigestStore(_DigestDb())
    monkeypatch.setattr(store, "async_list_active_subscriptions", fake_active_subscriptions)
    monkeypatch.setattr(store, "_batch_channel_run_states", fake_batch_run_states)

    assert await store.async_list_fetchable_subscriptions(1) == [due_subscription]
    assert digest_store_module._coerce_positive_int("4") == 4
    assert digest_store_module._coerce_positive_int("bad") is None
    assert digest_store_module._coerce_positive_int(0) is None
    assert digest_store_module._channel_source_due(
        {"is_active": True, "active_subscription": True, "backoff_until": now}
    )
    assert not digest_store_module._channel_source_due(
        {"is_active": True, "active_subscription": False}
    )


@pytest.mark.asyncio
async def test_fetchable_subscriptions_query_count_is_constant() -> None:
    """async_list_fetchable_subscriptions issues O(1) queries regardless of N."""
    past = datetime(2024, 1, 1, tzinfo=UTC)

    def _make(n: int) -> tuple[list[Any], list[Any], list[Any]]:
        subs, sources, source_subs = [], [], []
        for i in range(n):
            ch = Channel(id=10 + i, username=f"ch{i}", is_active=True)
            cs = ChannelSubscription(id=100 + i, user_id=1, channel_id=ch.id, is_active=True)
            cs.channel = ch
            subs.append(cs)
            sources.append(
                Source(
                    id=200 + i,
                    kind="telegram_channel",
                    external_id=f"ch{i}",
                    is_active=True,
                    metadata_json={},
                )
            )
            source_subs.append(
                Subscription(
                    id=300 + i, user_id=1, source_id=200 + i, is_active=True, next_fetch_at=past
                )
            )
        return subs, sources, source_subs

    async def run(n: int) -> int:
        subs, sources, source_subs = _make(n)
        # execute() returns, in order: active subscriptions, sources IN, subscriptions IN.
        db = _DigestDb(execute_rows=[subs, sources, source_subs])
        store = DigestStore(db)
        result = await store.async_list_fetchable_subscriptions(1)
        assert len(result) == n  # all due (next_fetch_at in the past)
        return len(db.session_obj.executed)

    # Same number of SQL statements for 2 channels and for 10 channels.
    assert await run(2) == await run(10)
