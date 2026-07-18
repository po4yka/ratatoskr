"""Postgres-backed tests for the tag repository."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import Request, Summary, SummaryTag, Tag, User
from app.db.session import Database
from app.infrastructure.persistence.repositories.tag_repository import (
    TagRepositoryAdapter,
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
        session.add(User(telegram_user_id=16001, username="tags"))
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(SummaryTag))
        await session.execute(delete(Tag))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _summary(database: Database, *, suffix: str) -> Summary:
    async with database.transaction() as session:
        request = Request(
            type="url",
            status="completed",
            correlation_id=f"tag-{suffix}",
            user_id=16001,
            input_url=f"https://example.com/tag/{suffix}",
            normalized_url=f"https://example.com/tag/{suffix}",
            dedupe_hash=f"tag-{suffix}",
        )
        session.add(request)
        await session.flush()
        summary = Summary(request_id=request.id, lang="en", json_payload={"summary_250": suffix})
        session.add(summary)
        await session.flush()
        return summary


@pytest.mark.asyncio
async def test_tag_repository_crud_attach_detach_restore(database: Database) -> None:
    repo = TagRepositoryAdapter(database)
    summary = await _summary(database, suffix="one")

    tag = await repo.async_create_tag(16001, "Machine Learning", "machine-learning", "#fff")
    assert tag["summary_count"] == 0
    updated = await repo.async_update_tag(tag["id"], "ML", "#000", user_id=16001)
    assert updated["name"] == "ML"
    assert updated["normalized_name"] == "ml"

    association = await repo.async_attach_tag(summary.id, tag["id"], "manual")
    duplicate = await repo.async_attach_tag(summary.id, tag["id"], "rule")
    assert duplicate["id"] == association["id"]
    assert [item["summary_count"] for item in await repo.async_get_user_tags(16001)] == [1]
    tags = await repo.async_get_tags_for_summary(summary.id)
    assert tags[0]["id"] == tag["id"]
    assert tags[0]["source"] == "manual"

    await repo.async_detach_tag(summary.id, tag["id"])
    assert await repo.async_get_tags_for_summary(summary.id) == []

    await repo.async_delete_tag(tag["id"], user_id=16001)
    assert await repo.async_get_tag_by_normalized_name(16001, "ml") is None
    deleted = await repo.async_get_tag_by_normalized_name(16001, "ml", include_deleted=True)
    assert deleted is not None
    restored = await repo.async_restore_tag(tag["id"], user_id=16001, name="Restored")
    assert restored["name"] == "Restored"
    assert restored["is_deleted"] is False


@pytest.mark.asyncio
async def test_tag_repository_tagged_summaries_and_merge(database: Database) -> None:
    repo = TagRepositoryAdapter(database)
    first_summary = await _summary(database, suffix="first")
    second_summary = await _summary(database, suffix="second")
    target = await repo.async_create_tag(16001, "Target", "target", None)
    source = await repo.async_create_tag(16001, "Source", "source", None)
    duplicate_source = await repo.async_create_tag(16001, "Duplicate", "duplicate", None)

    await repo.async_attach_tag(first_summary.id, target["id"], "manual")
    await repo.async_attach_tag(second_summary.id, source["id"], "manual")
    await repo.async_attach_tag(first_summary.id, duplicate_source["id"], "manual")
    await repo.async_merge_tags([source["id"], duplicate_source["id"]], target["id"], user_id=16001)

    tags = await repo.async_get_user_tags(16001)
    target_row = next(row for row in tags if row["id"] == target["id"])
    assert target_row["summary_count"] == 2
    assert {
        row["id"]
        for row in await repo.async_get_tagged_summaries(
            user_id=16001, tag_id=target["id"], limit=10
        )
    } == {
        first_summary.id,
        second_summary.id,
    }
    assert (await repo.async_get_tag_by_id(source["id"]))["is_deleted"] is True
