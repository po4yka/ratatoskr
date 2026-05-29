"""Shared database helpers for digest channel subscriptions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import Channel, ChannelSubscription
from app.db.runtime_database import resolve_runtime_database
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


async def async_subscribe_channel_atomic(
    user_id: int,
    username: str,
    *,
    db: Database | None = None,
) -> str:
    """Subscribe user to channel inside a PostgreSQL transaction."""
    database = db or _runtime_database()
    async with database.transaction() as session:
        channel = await session.scalar(select(Channel).where(Channel.username == username))
        if channel is None:
            channel = Channel(username=username, title=username, is_active=True)
            session.add(channel)
            await session.flush()

        existing = await session.scalar(
            select(ChannelSubscription).where(
                ChannelSubscription.user_id == user_id,
                ChannelSubscription.channel_id == channel.id,
            )
        )
        if existing is not None:
            if existing.is_active:
                return "already_subscribed"
            existing.is_active = True
            existing.updated_at = _utcnow()
            return "reactivated"

        session.add(
            ChannelSubscription(
                user_id=user_id,
                channel_id=channel.id,
                is_active=True,
            )
        )
        return "created"


async def async_unsubscribe_channel_atomic(
    user_id: int,
    username: str,
    *,
    db: Database | None = None,
) -> str:
    """Unsubscribe user from channel inside a PostgreSQL transaction."""
    database = db or _runtime_database()
    async with database.transaction() as session:
        channel = await session.scalar(select(Channel).where(Channel.username == username))
        if channel is None:
            return "not_found"

        subscription = await session.scalar(
            select(ChannelSubscription).where(
                ChannelSubscription.user_id == user_id,
                ChannelSubscription.channel_id == channel.id,
                ChannelSubscription.is_active.is_(True),
            )
        )
        if subscription is None:
            return "not_subscribed"

        subscription.is_active = False
        subscription.updated_at = _utcnow()
        return "unsubscribed"


def subscribe_channel_atomic(user_id: int, username: str, *, db: Database | None = None) -> str:
    """Synchronous compatibility wrapper for digest API facade call sites."""
    return asyncio.run(async_subscribe_channel_atomic(user_id, username, db=db))


def unsubscribe_channel_atomic(user_id: int, username: str, *, db: Database | None = None) -> str:
    """Synchronous compatibility wrapper for digest API facade call sites."""
    return asyncio.run(async_unsubscribe_channel_atomic(user_id, username, db=db))


def _runtime_database() -> Database:
    return resolve_runtime_database()
