"""Digest channel control persistence contracts."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from app.config.database import DatabaseConfig
from app.db.models import (
    Channel,
    ChannelPost,
    ChannelPostAnalysis,
    ChannelSubscription,
    DigestDelivery,
    FeedItem,
    Source,
    Subscription,
    User,
)
from app.db.session import Database
from app.infrastructure.persistence.digest_store import DigestStore

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
        await session.execute(delete(ChannelPostAnalysis))
        await session.execute(delete(ChannelPost))
        await session.execute(delete(DigestDelivery))
        await session.execute(delete(ChannelSubscription))
        await session.execute(delete(FeedItem))
        await session.execute(delete(Subscription))
        await session.execute(delete(Source))
        await session.execute(delete(Channel))
        await session.execute(delete(User))


async def _seed_channel(database: Database) -> Channel:
    async with database.transaction() as session:
        user = User(telegram_user_id=1001, username="owner")
        channel = Channel(username="examplechannel", title="Example Channel", is_active=True)
        session.add_all([user, channel])
        await session.flush()
        session.add(ChannelSubscription(user_id=1001, channel_id=channel.id, is_active=True))
        await session.flush()
        return channel


@pytest.mark.asyncio
async def test_digest_store_controls_channel_fetchability_and_manual_retry(
    database: Database,
) -> None:
    channel = await _seed_channel(database)
    store = DigestStore(database)

    disabled = await store.async_update_channel_controls(
        user_id=1001,
        username=channel.username,
        is_active=False,
        fetch_interval_seconds=900,
        max_items_per_run=4,
        retry_policy={"max_errors": 2},
    )
    disabled_state = await store.async_get_channel_run_state(user_id=1001, channel=channel)
    fetchable = await store.async_list_fetchable_subscriptions(1001)

    assert disabled is True
    assert disabled_state["is_active"] is False
    assert disabled_state["fetch_interval_seconds"] == 900
    assert disabled_state["max_items_per_run"] == 4
    assert disabled_state["retry_policy"] == {"max_errors": 2}
    assert fetchable == []

    retried = await store.async_retry_channel(user_id=1001, username=channel.username)
    retry_state = await store.async_get_channel_run_state(user_id=1001, channel=channel)

    assert retried is True
    assert retry_state["is_active"] is True
    assert retry_state["backoff_until"] is None


@pytest.mark.asyncio
async def test_digest_store_records_channel_error_and_source_backoff(
    database: Database,
) -> None:
    channel = await _seed_channel(database)
    store = DigestStore(database)
    await store.async_get_channel_run_state(user_id=1001, channel=channel)

    disabled = await store.async_record_channel_fetch_error(
        channel,
        "fetch_failed",
        max_errors=2,
    )
    state = await store.async_get_channel_run_state(user_id=1001, channel=channel)

    async with database.session() as session:
        source = await session.scalar(
            select(Source).where(
                Source.kind == "telegram_channel",
                Source.external_id == channel.username,
            )
        )

    assert disabled is False
    assert state["backoff_until"] is not None
    assert source is not None
    assert source.fetch_error_count == 1
    assert source.last_error == "fetch_failed"
