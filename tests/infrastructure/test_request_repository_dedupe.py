"""Tests for RequestRepositoryAdapter.async_find_recent_request_by_dedupe."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import CrawlResult, Request, Summary, TelegramMessage
from app.db.session import Database
from app.domain.models.request import RequestStatus
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
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
    async with db.transaction() as session:
        await session.execute(delete(TelegramMessage))
        await session.execute(delete(Summary))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Request))
    try:
        yield db
    finally:
        async with db.transaction() as session:
            await session.execute(delete(TelegramMessage))
            await session.execute(delete(Summary))
            await session.execute(delete(CrawlResult))
            await session.execute(delete(Request))
        await db.dispose()


@pytest.mark.asyncio
async def test_find_recent_returns_processing_row(database: Database) -> None:
    """Returns a row whose status is 'processing' within max_age_sec."""
    repo = RequestRepositoryAdapter(database)
    await repo.async_create_request(
        type_="url",
        status=cast("RequestStatus", "processing"),
        correlation_id="cid-proc-1",
        user_id=1,
        chat_id=1,
        dedupe_hash="hash-proc-1",
        input_url="https://example.com/proc",
    )

    result = await repo.async_find_recent_request_by_dedupe(
        "hash-proc-1", user_id=1, max_age_sec=300
    )

    assert result is not None
    assert result["status"] == "processing"
    assert result["correlation_id"] == "cid-proc-1"


@pytest.mark.asyncio
async def test_find_recent_returns_pending_row(database: Database) -> None:
    """Returns a row whose status is 'pending' within max_age_sec."""
    repo = RequestRepositoryAdapter(database)
    await repo.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="cid-pend-1",
        user_id=1,
        chat_id=1,
        dedupe_hash="hash-pend-1",
        input_url="https://example.com/pend",
    )

    result = await repo.async_find_recent_request_by_dedupe(
        "hash-pend-1", user_id=1, max_age_sec=300
    )

    assert result is not None
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_find_recent_returns_none_for_completed(database: Database) -> None:
    """Does not return a row whose status is 'completed'."""
    repo = RequestRepositoryAdapter(database)
    req_id = await repo.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="cid-done-1",
        user_id=1,
        chat_id=1,
        dedupe_hash="hash-done-1",
        input_url="https://example.com/done",
    )
    await repo.async_update_request_status(req_id, "completed")

    result = await repo.async_find_recent_request_by_dedupe(
        "hash-done-1", user_id=1, max_age_sec=300
    )

    assert result is None


@pytest.mark.asyncio
async def test_find_recent_returns_none_for_old_error(database: Database) -> None:
    """Does not return an error row older than max_age_sec."""
    repo = RequestRepositoryAdapter(database)
    req_id = await repo.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="cid-old-1",
        user_id=1,
        chat_id=1,
        dedupe_hash="hash-old-1",
        input_url="https://example.com/old",
    )
    await repo.async_update_request_error(req_id, status="error", error_type="timeout")

    # max_age_sec=0 means even a just-created row is too old
    result = await repo.async_find_recent_request_by_dedupe("hash-old-1", user_id=1, max_age_sec=0)

    assert result is None


@pytest.mark.asyncio
async def test_find_recent_returns_none_for_unknown_hash(database: Database) -> None:
    """Returns None when no row matches the dedupe_hash."""
    repo = RequestRepositoryAdapter(database)

    result = await repo.async_find_recent_request_by_dedupe(
        "hash-nonexistent-xyz", user_id=1, max_age_sec=300
    )

    assert result is None


@pytest.mark.asyncio
async def test_lookup_does_not_cross_owners(database: Database) -> None:
    """The predicate must run in SQL, against a real database.

    The x_bookmarks ingestor writes rows with a dedupe_hash and a NULL user_id,
    so an unscoped lookup could return one of those -- status X_IMPORTED, no
    summary -- instead of the owner's completed request, and the URL was
    summarized again. Rejecting the wrong row after the fact cannot recover the
    right one.
    """
    repo = RequestRepositoryAdapter(database)

    owner_id = await repo.async_create_request(
        type_="url",
        status=cast("RequestStatus", "pending"),
        correlation_id="owner-req",
        user_id=1,
        input_url="https://example.com/shared",
        normalized_url="https://example.com/shared",
        dedupe_hash="shared-hash",
    )
    # A bookmark-shaped row: same hash, no owner, terminal and summary-less.
    await repo.async_create_request(
        type_="x_bookmark",
        status=cast("RequestStatus", "x_imported"),
        correlation_id="bookmark-req",
        user_id=None,
        input_url="https://example.com/shared",
        normalized_url="https://example.com/shared",
        dedupe_hash="shared-hash",
    )

    found = await repo.async_get_request_by_dedupe_hash("shared-hash", user_id=1)

    assert found is not None
    assert found["id"] == owner_id
    assert found["user_id"] == 1

    # A different owner sees nothing rather than the other user's row.
    assert await repo.async_get_request_by_dedupe_hash("shared-hash", user_id=999) is None
