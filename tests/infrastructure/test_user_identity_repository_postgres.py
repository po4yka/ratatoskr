"""Postgres-backed tests for UserIdentityRepository magic-link consumption.

The key regression is the TOCTOU race in async_consume_magic_link: the old
SELECT-then-write could let two concurrent requests redeem the same token twice.
The atomic conditional UPDATE must guarantee exactly one winner. A meaningful
concurrency test needs a real Postgres (row-locking + READ COMMITTED semantics)
and a pool large enough for the gathered transactions to hold separate
connections, so these are gated on TEST_DATABASE_URL and skip otherwise.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import MagicLinkToken, User
from app.db.session import Database
from app.infrastructure.persistence.repositories.user_identity_repository import (
    UserIdentityRepository,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_USER_ID = 77001


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    # pool_size >= 2 so two gathered consume() transactions get distinct
    # connections and genuinely race, rather than serializing on one connection.
    db = Database(DatabaseConfig(dsn=dsn, pool_size=4, max_overflow=2))
    await db.migrate()
    await _clear(db)
    async with db.transaction() as session:
        session.add(User(telegram_user_id=_USER_ID, username="magic", is_owner=True))
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(MagicLinkToken))
        await session.execute(delete(User).where(User.telegram_user_id == _USER_ID))


async def _issue(repo: UserIdentityRepository, **overrides: object) -> str:
    kwargs = {
        "user_id": _USER_ID,
        "email": "magic@example.com",
        "email_canonical": "magic@example.com",
        "client_id": "web-v1",
    }
    kwargs.update(overrides)
    issue = await repo.async_issue_magic_link(**kwargs)  # type: ignore[arg-type]
    return issue.token


@pytest.mark.asyncio
async def test_concurrent_consume_redeems_token_exactly_once(database: Database) -> None:
    repo = UserIdentityRepository(database)
    token = await _issue(repo)

    # Fire many concurrent consumptions of the same token.
    results = await asyncio.gather(*(repo.async_consume_magic_link(token) for _ in range(8)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"token redeemed {len(winners)} times, expected exactly 1"
    assert winners[0]["user_id"] == _USER_ID
    assert winners[0]["email_canonical"] == "magic@example.com"
    assert winners[0]["client_id"] == "web-v1"


@pytest.mark.asyncio
async def test_sequential_second_consume_returns_none(database: Database) -> None:
    repo = UserIdentityRepository(database)
    token = await _issue(repo)

    first = await repo.async_consume_magic_link(token)
    second = await repo.async_consume_magic_link(token)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_unknown_token_returns_none(database: Database) -> None:
    repo = UserIdentityRepository(database)
    assert await repo.async_consume_magic_link("not-a-real-token") is None


@pytest.mark.asyncio
async def test_expired_token_is_not_consumable(database: Database) -> None:
    repo = UserIdentityRepository(database)
    token = await _issue(repo, ttl=timedelta(seconds=-1))
    assert await repo.async_consume_magic_link(token) is None
