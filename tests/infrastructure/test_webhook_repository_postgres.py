"""Postgres-backed tests for the webhook repository."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import User, WebhookDelivery, WebhookSubscription
from app.db.session import Database
from app.infrastructure.persistence.repositories.webhook_repository import (
    WebhookRepositoryAdapter,
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
    async with db.transaction() as session:
        session.add(User(telegram_user_id=14001, username="webhook"))
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(WebhookDelivery))
        await session.execute(delete(WebhookSubscription))
        await session.execute(delete(User))


@pytest.mark.asyncio
async def test_webhook_repository_subscription_lifecycle(database: Database) -> None:
    repo = WebhookRepositoryAdapter(database)

    sub = await repo.async_create_subscription(
        14001,
        name="primary",
        url="https://example.com/hook",
        secret="old",
        events=["summary.created"],
    )
    updated = await repo.async_update_subscription(
        sub["id"],
        user_id=14001,
        name="updated",
        events=["summary.created", "summary.failed"],
        enabled=False,
    )
    assert updated["name"] == "updated"
    assert updated["events_json"] == ["summary.created", "summary.failed"]
    assert updated["enabled"] is False

    await repo.async_rotate_secret(sub["id"], "new", user_id=14001)
    await repo.async_disable_subscription(sub["id"])
    loaded = await repo.async_get_subscription_by_id(sub["id"])
    assert loaded is not None
    assert loaded["secret"] == "new"
    assert loaded["status"] == "disabled"
    assert loaded["enabled"] is False
    assert await repo.async_get_user_subscriptions(14001) == []
    assert [row["id"] for row in await repo.async_get_user_subscriptions(14001, False)] == [
        sub["id"]
    ]

    await repo.async_delete_subscription(sub["id"], user_id=14001)
    assert await repo.async_get_user_subscriptions(14001, False) == []


@pytest.mark.asyncio
async def test_webhook_repository_delivery_and_failure_counters(database: Database) -> None:
    repo = WebhookRepositoryAdapter(database)
    sub = await repo.async_create_subscription(
        14001,
        name="primary",
        url="https://example.com/hook",
        secret="secret",
        events=["summary.created"],
    )

    delivery = await repo.async_log_delivery(
        sub["id"],
        event_type="summary.created",
        payload={"summary_id": 1},
        response_status=200,
        response_body="ok",
        duration_ms=50,
        success=True,
        attempt=1,
        error=None,
    )
    deliveries = await repo.async_get_deliveries(sub["id"])
    assert [row["id"] for row in deliveries] == [delivery["id"]]
    assert deliveries[0]["payload_json"] == {"summary_id": 1}

    assert await repo.async_increment_failure_count(sub["id"]) == 1
    assert await repo.async_increment_failure_count(sub["id"]) == 2
    await repo.async_reset_failure_count(sub["id"])
    loaded = await repo.async_get_subscription_by_id(sub["id"])
    assert loaded is not None
    assert loaded["failure_count"] == 0
    assert loaded["last_delivery_at"] is not None
