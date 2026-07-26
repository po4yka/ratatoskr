from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, func, select

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import Collection, CollectionItem, Request, Summary, SummaryTag, Tag, User
from app.db.session import Database
from app.domain.services.import_parsers.base import ImportedBookmark
from app.infrastructure.persistence.repositories.bookmark_import_repository import (
    BookmarkImportAdapter,
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
        await session.execute(delete(CollectionItem))
        await session.execute(delete(SummaryTag))
        await session.execute(delete(Summary))
        await session.execute(delete(Collection))
        await session.execute(delete(Tag))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _create_user_and_collection(database: Database) -> int:
    async with database.transaction() as session:
        session.add(User(telegram_user_id=9101, username="importer"))
        collection = Collection(user_id=9101, name="Reading")
        session.add(collection)
        await session.flush()
        return collection.id


@pytest.mark.asyncio
async def test_bookmark_import_repository_creates_summary_tags_and_collection_item(
    database: Database,
) -> None:
    collection_id = await _create_user_and_collection(database)
    repo = BookmarkImportAdapter(database)
    bookmark = ImportedBookmark(
        url="https://example.com/bookmark?utm_source=test",
        title="Bookmark",
        tags=["Postgres", "postgres", "  "],
        notes="Useful migration note",
        created_at=dt.datetime.now(UTC) - dt.timedelta(days=1),
    )

    result = await repo.async_import_bookmark(
        bookmark,
        user_id=9101,
        options={"target_collection_id": collection_id},
    )
    duplicate = await repo.async_import_bookmark(bookmark, user_id=9101, options={})

    assert result.outcome == "created"
    assert duplicate.outcome == "skipped"
    async with database.session() as session:
        request = await session.scalar(select(Request).where(Request.user_id == 9101))
        assert request is not None
        assert request.status == "completed"
        assert request.content_text == "Useful migration note"

        summary = await session.scalar(select(Summary).where(Summary.request_id == request.id))
        assert summary is not None
        assert summary.json_payload == {
            "title": "Bookmark",
            "summary_250": "Useful migration note",
            "topic_tags": ["Postgres", "postgres", "  "],
        }
        assert await session.scalar(select(func.count(Tag.id))) == 1
        assert await session.scalar(select(func.count(SummaryTag.id))) == 1
        assert await session.scalar(select(func.count(CollectionItem.id))) == 1


@pytest.mark.asyncio
async def test_bookmark_import_repository_rejects_unowned_target_collection(
    database: Database,
) -> None:
    collection_id = await _create_user_and_collection(database)
    repo = BookmarkImportAdapter(database)

    with pytest.raises(ValueError, match="not found or not owned"):
        await repo.async_import_bookmark(
            ImportedBookmark(url="https://example.com/other"),
            user_id=9999,
            options={"target_collection_id": collection_id},
        )


@pytest.mark.asyncio
async def test_bookmark_import_repository_can_skip_source_tags(database: Database) -> None:
    await _create_user_and_collection(database)
    repo = BookmarkImportAdapter(database)

    result = await repo.async_import_bookmark(
        ImportedBookmark(
            url="https://example.com/no-tags",
            title="No tags",
            tags=["source-tag"],
        ),
        user_id=9101,
        options={"create_tags": False},
    )

    assert result.outcome == "created"
    async with database.session() as session:
        assert await session.scalar(select(func.count(Tag.id))) == 0
        assert await session.scalar(select(func.count(SummaryTag.id))) == 0
