"""Postgres-backed behavioral tests for the summary repository."""

from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    AggregationSession,
    AggregationSessionItem,
    CrawlResult,
    Request,
    Summary,
    SummaryFeedback,
    TopicSearchIndex,
    User,
)
from app.db.session import Database
from app.domain.models.request import RequestStatus
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
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
        await session.execute(delete(AggregationSessionItem))
        await session.execute(delete(AggregationSession))
        await session.execute(delete(SummaryFeedback))
        await session.execute(delete(TopicSearchIndex))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _create_request(
    database: Database,
    *,
    user_id: int = 101,
    chat_id: int = 202,
    url: str = "https://example.com/summary",
    status: str = "pending",
) -> int:
    async with database.transaction() as session:
        await session.execute(
            insert(User)
            .values(telegram_user_id=user_id, username=f"user-{user_id}")
            .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
        )
        request = Request(
            type="url",
            status=status,
            correlation_id=f"summary-{user_id}-{url}",
            user_id=user_id,
            chat_id=chat_id,
            input_url=url,
            normalized_url=url,
            dedupe_hash=f"summary-{user_id}-{url}",
            content_text="postgres sqlalchemy topic",
        )
        session.add(request)
        await session.flush()
        return request.id


async def _create_summary_for_user(
    database: Database,
    repo: SummaryRepositoryAdapter,
    *,
    user_id: int,
    url: str,
    is_read: bool = False,
) -> int:
    request_id = await _create_request(database, user_id=user_id, url=url, status="completed")
    await repo.async_upsert_summary(
        request_id,
        "en",
        {"summary_250": f"summary for {url}"},
        is_read=is_read,
    )
    summary = await repo.async_get_summary_by_request(request_id)
    assert summary is not None
    return int(summary["id"])


@pytest.mark.asyncio
async def test_summary_repository_upserts_reads_and_finalizes(database: Database) -> None:
    repo = SummaryRepositoryAdapter(database)
    request_id = await _create_request(database)

    v1 = await repo.async_upsert_summary(
        request_id,
        "en",
        {"summary_250": "first", "topic_tags": ["postgres"]},
        {"score": 1},
    )
    v2 = await repo.async_finalize_request_summary(
        request_id,
        "en",
        {"summary_250": "second", "topic_tags": ["postgres", "sqlalchemy"]},
        {"score": 2},
        request_status=RequestStatus.COMPLETED,
    )

    assert v2 == v1 + 1
    summary = await repo.async_get_summary_by_request(request_id)
    assert summary is not None
    assert summary["json_payload"]["summary_250"] == "second"
    assert await repo.async_get_summary_id_by_request(request_id) == summary["id"]

    by_id = await repo.async_get_summary_by_id(summary["id"])
    assert by_id is not None
    assert by_id["user_id"] == 101

    async with database.session() as session:
        request_status = await session.scalar(
            select(Request.status).where(Request.id == request_id)
        )
    assert request_status == RequestStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_summary_repository_context_state_sync_and_feedback(database: Database) -> None:
    repo = SummaryRepositoryAdapter(database)
    request_id = await _create_request(database, user_id=303, chat_id=404)
    summary_version = await repo.async_upsert_summary(
        request_id,
        "en",
        {"summary_250": "body", "key_ideas": ["one"]},
    )
    summary = await repo.async_get_summary_by_request(request_id)
    assert summary is not None
    summary_id = summary["id"]

    async with database.transaction() as session:
        session.add(
            CrawlResult(
                request_id=request_id,
                firecrawl_success=True,
                content_markdown="body",
                metadata_json={"source": "test"},
            )
        )

    context = await repo.async_get_summary_context_by_id(summary_id)
    assert context is not None
    assert context["summary"]["id"] == summary_id
    assert context["request"]["id"] == request_id
    assert context["crawl_result"]["request_id"] == request_id

    await repo.async_mark_summary_as_read(summary_id)
    assert await repo.async_get_read_status(request_id) is True
    assert await repo.async_get_unread_summary_by_request_id(request_id) is None
    await repo.async_mark_summary_as_unread(summary_id)
    assert await repo.async_get_read_status(request_id) is False
    assert (await repo.async_get_unread_summary_by_request_id(request_id))["id"] == summary_id

    await repo.async_update_reading_progress(summary_id, 0.5, 123)
    assert await repo.async_toggle_favorite(summary_id) is True
    await repo.async_set_favorite(summary_id, False)
    assert await repo.async_apply_sync_change(summary_id, is_read=True) >= summary_version
    assert (await repo.async_get_summary_for_sync_apply(summary_id, 303))["id"] == summary_id

    feedback = await repo.async_upsert_feedback(303, summary_id, 5, ["clear"], "good")
    updated_feedback = await repo.async_upsert_feedback(303, summary_id, None, None, "still good")
    assert feedback["id"] == updated_feedback["id"]
    assert updated_feedback["rating"] == 5
    assert updated_feedback["issues"] == ["clear"]
    assert updated_feedback["comment"] == "still good"


@pytest.mark.asyncio
async def test_summary_repository_bulk_mark_read_skips_cross_user_ids(database: Database) -> None:
    repo = SummaryRepositoryAdapter(database)
    owned_first = await _create_summary_for_user(
        database, repo, user_id=7101, url="https://bulk.example/owned-1"
    )
    owned_second = await _create_summary_for_user(
        database, repo, user_id=7101, url="https://bulk.example/owned-2"
    )
    owned_third = await _create_summary_for_user(
        database, repo, user_id=7101, url="https://bulk.example/owned-3"
    )
    other = await _create_summary_for_user(
        database, repo, user_id=7102, url="https://bulk.example/other"
    )

    assert (
        await repo.async_bulk_mark_summaries_as_read(
            user_id=7101, summary_ids=[owned_first, owned_second]
        )
        == 2
    )
    assert (
        await repo.async_bulk_mark_summaries_as_read(
            user_id=7101, summary_ids=[owned_third, other, owned_third]
        )
        == 1
    )
    assert await repo.async_bulk_mark_summaries_as_read(user_id=7101, summary_ids=[other]) == 0

    async with database.session() as session:
        rows = await session.execute(
            select(Summary.id, Summary.is_read).where(
                Summary.id.in_([owned_first, owned_second, owned_third, other])
            )
        )
        states = {row[0]: row[1] for row in rows}
    assert states == {owned_first: True, owned_second: True, owned_third: True, other: False}


@pytest.mark.asyncio
async def test_summary_repository_bulk_favorite_skips_cross_user_ids_and_handles_duplicates(
    database: Database,
) -> None:
    repo = SummaryRepositoryAdapter(database)
    owned = await _create_summary_for_user(
        database, repo, user_id=7201, url="https://bulk.example/fav-owned"
    )
    other = await _create_summary_for_user(
        database, repo, user_id=7202, url="https://bulk.example/fav-other"
    )

    assert (
        await repo.async_bulk_set_summaries_favorite(
            user_id=7201, summary_ids=[owned, owned, other], value=True
        )
        == 1
    )
    assert (
        await repo.async_bulk_set_summaries_favorite(user_id=7201, summary_ids=[other], value=True)
        == 0
    )

    async with database.session() as session:
        rows = await session.execute(
            select(Summary.id, Summary.is_favorited).where(Summary.id.in_([owned, other]))
        )
        states = {row[0]: row[1] for row in rows}
    assert states == {owned: True, other: False}


@pytest.mark.asyncio
async def test_summary_repository_bulk_delete_skips_cross_user_ids(database: Database) -> None:
    repo = SummaryRepositoryAdapter(database)
    owned = await _create_summary_for_user(
        database, repo, user_id=7301, url="https://bulk.example/delete-owned"
    )
    other = await _create_summary_for_user(
        database, repo, user_id=7302, url="https://bulk.example/delete-other"
    )

    assert (
        await repo.async_bulk_soft_delete_summaries(user_id=7301, summary_ids=[owned, other]) == 1
    )
    assert await repo.async_bulk_soft_delete_summaries(user_id=7301, summary_ids=[other]) == 0

    async with database.session() as session:
        rows = await session.execute(
            select(Summary.id, Summary.is_deleted).where(Summary.id.in_([owned, other]))
        )
        states = {row[0]: row[1] for row in rows}
    assert states == {owned: True, other: False}


@pytest.mark.asyncio
async def test_aggregation_source_bundle_is_scoped_to_summary_owner(
    database: Database,
) -> None:
    repo = SummaryRepositoryAdapter(database)
    owned_request_id = await _create_request(
        database,
        user_id=7401,
        url="https://bundle.example/owned",
        status="completed",
    )
    await repo.async_upsert_summary(owned_request_id, "en", {"summary_250": "owned"})
    owned_summary = await repo.async_get_summary_by_request(owned_request_id)
    assert owned_summary is not None
    async with database.transaction() as session:
        await session.execute(
            insert(User)
            .values(telegram_user_id=7402, username="user-7402")
            .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
        )
        other_session = AggregationSession(
            user_id=7402,
            correlation_id="cross-user-bundle",
            total_items=1,
            status="completed",
        )
        session.add(other_session)
        await session.flush()
        session.add(
            AggregationSessionItem(
                aggregation_session_id=other_session.id,
                request_id=owned_request_id,
                position=0,
                source_kind="url",
                source_item_id="owned",
                source_dedupe_key="owned",
                status="completed",
            )
        )

    legacy_unscoped = await repo.async_get_aggregation_source_bundle_for_summary(
        int(owned_summary["id"])
    )
    assert legacy_unscoped is not None
    assert legacy_unscoped["session"]["user_id"] == 7402
    assert (
        await repo.async_get_aggregation_source_bundle_for_summary_owned_by_user(
            int(owned_summary["id"]),
            7401,
        )
        is None
    )

@pytest.mark.asyncio
async def test_summary_repository_user_lists_and_topic_filter(database: Database) -> None:
    repo = SummaryRepositoryAdapter(database)
    first_request_id = await _create_request(
        database, user_id=505, url="https://example.com/postgres"
    )
    second_request_id = await _create_request(
        database, user_id=505, url="https://example.com/other"
    )
    first_version = await repo.async_upsert_summary(
        first_request_id,
        "en",
        {"summary_250": "Postgres storage migration", "topic_tags": ["postgres"]},
    )
    await repo.async_upsert_summary(
        second_request_id,
        "en",
        {"summary_250": "Unrelated mobile update", "topic_tags": ["mobile"]},
        is_read=True,
    )
    first_summary = await repo.async_get_summary_by_request(first_request_id)
    assert first_summary is not None

    async with database.transaction() as session:
        session.add(
            TopicSearchIndex(
                request_id=first_request_id,
                url="https://example.com/postgres",
                title="Postgres migration",
                snippet="Postgres storage",
                body="Postgres storage migration",
                tags="postgres",
            )
        )

    summaries, total, unread = await repo.async_get_user_summaries(505, limit=10)
    assert total == 2
    assert unread == 1
    assert [row["request"]["id"] for row in summaries] == [second_request_id, first_request_id]

    unread_topic_ids = [
        row["id"] for row in await repo.async_get_unread_summaries(505, None, topic="postgres")
    ]
    assert unread_topic_ids == [first_summary["id"]]
    assert (await repo.async_get_summaries_by_request_ids([first_request_id]))[first_request_id][
        "id"
    ] == first_summary["id"]
    assert [row["id"] for row in await repo.async_get_all_for_user(505)] == [
        first_summary["id"],
        (await repo.async_get_summary_by_request(second_request_id))["id"],
    ]
    assert await repo.async_get_max_server_version(505) is not None
    insight_rows = await repo.async_get_user_summaries_for_insights(
        505, dt.datetime.now(UTC) - dt.timedelta(days=1), 5
    )
    first_insight_row = next(row for row in insight_rows if row["request_id"] == first_request_id)
    assert first_insight_row["version"] == first_version
    assert first_insight_row["json_payload"]["topic_tags"] == ["postgres"]
    assert first_insight_row["request"]["created_at"] is not None
    assert "insights_json" not in first_insight_row
    assert (
        len(
            await repo.async_get_user_summary_activity_dates(
                505, dt.datetime.now(UTC) - dt.timedelta(days=1)
            )
        )
        == 2
    )
