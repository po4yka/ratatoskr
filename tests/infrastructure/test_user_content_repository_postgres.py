from __future__ import annotations

import datetime as dt
import json
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    Collection,
    CollectionItem,
    CustomDigest,
    Request,
    Summary,
    SummaryHighlight,
    SummaryTag,
    Tag,
    User,
    UserGoal,
)
from app.db.session import Database
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
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
        await session.execute(delete(SummaryHighlight))
        await session.execute(delete(CustomDigest))
        await session.execute(delete(UserGoal))
        await session.execute(delete(CollectionItem))
        await session.execute(delete(SummaryTag))
        await session.execute(delete(Collection))
        await session.execute(delete(Tag))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _seed_content(database: Database) -> dict[str, int]:
    async with database.transaction() as session:
        user = User(telegram_user_id=9501, username="reader")
        other_user = User(telegram_user_id=9502, username="other")
        session.add_all([user, other_user])
        request = Request(
            type="url",
            status="completed",
            correlation_id="content-request",
            user_id=user.telegram_user_id,
            input_url="https://example.com/article",
            normalized_url="https://example.com/article",
            dedupe_hash="content-request",
        )
        other_request = Request(
            type="url",
            status="completed",
            correlation_id="other-request",
            user_id=other_user.telegram_user_id,
            input_url="https://example.com/other",
            normalized_url="https://example.com/other",
            dedupe_hash="other-request",
        )
        session.add_all([request, other_request])
        await session.flush()
        summary = Summary(
            request_id=request.id,
            lang="en",
            json_payload={"title": "Article", "summary_250": "body"},
            created_at=dt.datetime.now(UTC),
        )
        other_summary = Summary(
            request_id=other_request.id,
            lang="en",
            json_payload={"title": "Other"},
        )
        session.add_all([summary, other_summary])
        await session.flush()
        tag = Tag(user_id=user.telegram_user_id, name="Postgres", normalized_name="postgres")
        collection = Collection(user_id=user.telegram_user_id, name="Inbox")
        session.add_all([tag, collection])
        await session.flush()
        session.add_all(
            [
                SummaryTag(summary_id=summary.id, tag_id=tag.id, source="manual"),
                CollectionItem(collection_id=collection.id, summary_id=summary.id, position=0),
            ]
        )
        return {
            "user_id": user.telegram_user_id,
            "other_user_id": other_user.telegram_user_id,
            "summary_id": summary.id,
            "other_summary_id": other_summary.id,
            "tag_id": tag.id,
            "collection_id": collection.id,
        }


@pytest.mark.asyncio
async def test_user_content_repository_goals_and_scope_counts(database: Database) -> None:
    ids = await _seed_content(database)
    repo = UserContentRepositoryAdapter(database)
    start = dt.datetime.now(UTC) - dt.timedelta(days=1)
    end = dt.datetime.now(UTC) + dt.timedelta(days=1)

    goal = await repo.async_upsert_goal(
        user_id=ids["user_id"],
        goal_type="weekly_reading",
        scope_type="global",
        scope_id=None,
        target_count=3,
    )
    updated = await repo.async_upsert_goal(
        user_id=ids["user_id"],
        goal_type="weekly_reading",
        scope_type="global",
        scope_id=None,
        target_count=5,
    )

    assert updated["id"] == goal["id"]
    assert updated["target_count"] == 5
    assert [row["id"] for row in await repo.async_list_goals(ids["user_id"])] == [goal["id"]]
    assert (
        await repo.async_get_scope_name(
            user_id=ids["user_id"], scope_type="tag", scope_id=ids["tag_id"]
        )
        == "Postgres"
    )
    assert (
        await repo.async_get_scope_name(
            user_id=ids["user_id"],
            scope_type="collection",
            scope_id=ids["collection_id"],
        )
        == "Inbox"
    )
    assert (
        await repo.async_count_scoped_summaries_in_period(
            user_id=ids["user_id"],
            start=start,
            end=end,
            scope_type="tag",
            scope_id=ids["tag_id"],
        )
        == 1
    )
    assert (
        await repo.async_count_scoped_summaries_in_period(
            user_id=ids["user_id"],
            start=start,
            end=end,
            scope_type="collection",
            scope_id=ids["collection_id"],
        )
        == 1
    )
    assert (
        await repo.async_delete_global_goal(user_id=ids["user_id"], goal_type="weekly_reading") == 1
    )


@pytest.mark.asyncio
async def test_user_content_repository_digests_highlights_and_owned_summaries(
    database: Database,
) -> None:
    ids = await _seed_content(database)
    repo = UserContentRepositoryAdapter(database)

    owned = await repo.async_get_owned_summaries(
        user_id=ids["user_id"],
        summary_ids=[ids["summary_id"], ids["other_summary_id"]],
    )
    assert [row["id"] for row in owned] == [ids["summary_id"]]
    assert owned[0]["request"]["input_url"] == "https://example.com/article"
    assert (
        await repo.async_get_owned_summary(
            user_id=ids["other_user_id"], summary_id=ids["summary_id"]
        )
        is None
    )

    digest = await repo.async_create_custom_digest(
        user_id=ids["user_id"],
        title="Digest",
        summary_ids=[ids["summary_id"]],
        format="markdown",
        content="# Digest",
    )
    assert json.loads(digest["summary_ids"]) == [str(ids["summary_id"])]
    assert [row["id"] for row in await repo.async_list_custom_digests(ids["user_id"])] == [
        digest["id"]
    ]
    assert (await repo.async_get_custom_digest(str(digest["id"])))["title"] == "Digest"

    highlight = await repo.async_create_highlight(
        user_id=ids["user_id"],
        summary_id=ids["summary_id"],
        text="important",
        start_offset=0,
        end_offset=9,
        color="yellow",
        note=None,
    )
    loaded_highlight = await repo.async_get_highlight(
        user_id=ids["user_id"],
        summary_id=ids["summary_id"],
        highlight_id=str(highlight["id"]),
    )
    assert loaded_highlight is not None
    assert loaded_highlight["text"] == "important"
    updated_highlight = await repo.async_update_highlight(
        highlight_id=str(highlight["id"]),
        color="green",
        note="saved",
    )
    assert updated_highlight["color"] == "green"
    assert updated_highlight["note"] == "saved"
    assert (
        len(await repo.async_list_highlights(user_id=ids["user_id"], summary_id=ids["summary_id"]))
        == 1
    )
    await repo.async_delete_highlight(str(highlight["id"]))
    assert (
        await repo.async_list_highlights(user_id=ids["user_id"], summary_id=ids["summary_id"]) == []
    )


@pytest.mark.asyncio
async def test_user_content_repository_exports_filtered_summaries(database: Database) -> None:
    ids = await _seed_content(database)
    repo = UserContentRepositoryAdapter(database)

    tagged = await repo.async_export_summaries(
        user_id=ids["user_id"],
        tag="Postgres",
        collection_id=None,
    )
    assert len(tagged) == 1
    assert tagged[0]["title"] == "Article"
    assert tagged[0]["url"] == "https://example.com/article"
    assert tagged[0]["tags"] == [{"name": "Postgres"}]
    assert tagged[0]["collections"] == [{"name": "Inbox"}]

    collection = await repo.async_export_summaries(
        user_id=ids["user_id"],
        tag=None,
        collection_id=ids["collection_id"],
    )
    assert [row["id"] for row in collection] == [ids["summary_id"]]
    assert (
        await repo.async_export_summaries(
            user_id=ids["user_id"],
            tag="Missing",
            collection_id=None,
        )
        == []
    )
