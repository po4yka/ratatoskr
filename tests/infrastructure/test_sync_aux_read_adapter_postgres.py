from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import Request, Summary, SummaryHighlight, SummaryTag, Tag, User
from app.db.session import Database
from app.infrastructure.persistence.sync_aux_read_adapter import SyncAuxReadAdapter

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
        await session.execute(delete(SummaryTag))
        await session.execute(delete(Tag))
        await session.execute(delete(SummaryHighlight))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


@pytest.mark.asyncio
async def test_sync_aux_read_adapter_reads_user_scoped_records(database: Database) -> None:
    async with database.transaction() as session:
        user = User(telegram_user_id=8601, username="sync-owner")
        other = User(telegram_user_id=8602, username="sync-other")
        session.add_all([user, other])
        await session.flush()

        request = Request(
            user_id=user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/sync",
            normalized_url="https://example.com/sync",
            dedupe_hash="sync-aux-1",
        )
        other_request = Request(
            user_id=other.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/other",
            normalized_url="https://example.com/other",
            dedupe_hash="sync-aux-2",
        )
        session.add_all([request, other_request])
        await session.flush()

        summary = Summary(request_id=request.id, lang="en", json_payload={"title": "Sync"})
        other_summary = Summary(
            request_id=other_request.id,
            lang="en",
            json_payload={"title": "Other"},
        )
        session.add_all([summary, other_summary])
        await session.flush()

        highlight = SummaryHighlight(
            user_id=user.telegram_user_id,
            summary_id=summary.id,
            text="important",
        )
        tag = Tag(
            user_id=user.telegram_user_id,
            name="Sync",
            normalized_name="sync",
        )
        other_tag = Tag(
            user_id=other.telegram_user_id,
            name="Other",
            normalized_name="other",
        )
        session.add_all([highlight, tag, other_tag])
        await session.flush()
        session.add(SummaryTag(summary_id=summary.id, tag_id=tag.id, source="manual"))
        session.add(SummaryTag(summary_id=other_summary.id, tag_id=other_tag.id, source="manual"))

    adapter = SyncAuxReadAdapter(database)

    highlights = await adapter.get_highlights_for_user(user.telegram_user_id)
    tags = await adapter.get_tags_for_user(user.telegram_user_id)
    summary_tags = await adapter.get_summary_tags_for_user(user.telegram_user_id)

    assert [row["text"] for row in highlights] == ["important"]
    assert [row["name"] for row in tags] == ["Sync"]
    assert len(summary_tags) == 1
    assert summary_tags[0]["tag_id"] == tag.id


@pytest.mark.asyncio
async def test_sync_page_applies_user_scope_cursor_order_and_limit(database: Database) -> None:
    user_id = 8701
    other_user_id = 8702
    async with database.transaction() as session:
        user = User(telegram_user_id=user_id, username="sync-page-owner")
        other = User(telegram_user_id=other_user_id, username="sync-page-other")
        session.add_all([user, other])
        await session.flush()

        requests = [
            Request(
                user_id=user_id,
                type="url",
                status="completed",
                input_url=f"https://example.com/sync/{version}",
                normalized_url=f"https://example.com/sync/{version}",
                dedupe_hash=f"sync-page-{version}",
                server_version=version,
            )
            for version in (10, 20, 30)
        ]
        requests.append(
            Request(
                user_id=other_user_id,
                type="url",
                status="completed",
                input_url="https://example.com/sync/other",
                normalized_url="https://example.com/sync/other",
                dedupe_hash="sync-page-other",
                server_version=15,
            )
        )
        session.add_all(requests)

    adapter = SyncAuxReadAdapter(database)

    first = await adapter.get_sync_page(
        "request",
        user_id,
        since=10,
        limit=1,
        through_version=None,
    )
    bounded = await adapter.get_sync_page(
        "request",
        user_id,
        since=10,
        limit=None,
        through_version=20,
    )

    assert [row["server_version"] for row in first] == [20]
    assert [row["server_version"] for row in bounded] == [20]
    assert "content_text" not in first[0]
