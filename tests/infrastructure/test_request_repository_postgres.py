from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
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
async def test_request_repository_create_update_and_read(database: Database) -> None:
    repo = RequestRepositoryAdapter(database)

    request_id = await repo.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="repo-request",
        user_id=42,
        chat_id=101,
        input_url="https://example.com",
        normalized_url="https://example.com/",
        dedupe_hash="repo-request-hash",
        input_message_id=11,
        fwd_from_chat_id=202,
        fwd_from_msg_id=303,
    )
    duplicate_id = await repo.async_create_request(
        status=RequestStatus.CRAWLING,
        correlation_id="repo-request-updated",
        user_id=42,
        input_url="https://example.com/updated",
        normalized_url="https://example.com/updated",
        dedupe_hash="repo-request-hash",
    )

    assert duplicate_id == request_id
    row = await repo.async_get_request_by_id(request_id)
    assert row is not None
    assert row["status"] == RequestStatus.PENDING.value
    assert row["correlation_id"] == "repo-request"
    assert await repo.async_get_request_by_dedupe_hash("repo-request-hash") == row

    await repo.async_update_request_error(
        request_id,
        "error",
        error_type="TEST",
        error_message="failed",
        processing_time_ms=99,
        error_context_json={"stage": "repo"},
    )
    assert await repo.async_get_request_error_context(request_id) == {"stage": "repo"}
    assert await repo.async_count_pending_requests_before(dt.datetime.now(UTC)) == 0
    assert await repo.async_get_max_server_version(42) is not None


@pytest.mark.asyncio
async def test_request_repository_context_and_telegram_message(database: Database) -> None:
    repo = RequestRepositoryAdapter(database)
    request_id, created = await repo.async_create_minimal_request(
        user_id=43,
        chat_id=101,
        input_url="https://example.com/context",
        normalized_url="https://example.com/context",
        dedupe_hash="repo-context-hash",
    )
    same_request_id, created_again = await repo.async_create_minimal_request(
        user_id=43, dedupe_hash="repo-context-hash"
    )

    assert created is True
    assert (same_request_id, created_again) == (request_id, False)

    async with database.transaction() as session:
        session.add(
            CrawlResult(
                request_id=request_id,
                firecrawl_success=True,
                content_markdown="body",
                metadata_json={"source": "test"},
            )
        )
        session.add(Summary(request_id=request_id, lang="en", json_payload={"summary_250": "s"}))

    telegram_id = await repo.async_insert_telegram_message(
        request_id=request_id,
        message_id=77,
        chat_id=101,
        date_ts=123,
        text_full="hello",
        entities_json=[],
        media_type=None,
        media_file_ids_json=[],
        forward_from_chat_id=None,
        forward_from_chat_type=None,
        forward_from_chat_title=None,
        forward_from_message_id=None,
        forward_date_ts=None,
        telegram_raw_json={"id": 77},
    )
    assert (
        await repo.async_insert_telegram_message(
            request_id=request_id,
            message_id=77,
            chat_id=101,
            date_ts=123,
            text_full="hello",
            entities_json=[],
            media_type=None,
            media_file_ids_json=[],
            forward_from_chat_id=None,
            forward_from_chat_type=None,
            forward_from_chat_title=None,
            forward_from_message_id=None,
            forward_date_ts=None,
            telegram_raw_json={"id": 77},
        )
        == telegram_id
    )

    await repo.async_update_bot_reply_message_id(request_id, 88)
    matched = await repo.async_get_request_by_telegram_message(user_id=43, message_id=88)
    assert matched is not None
    assert matched["id"] == request_id

    context = await repo.async_get_request_context(request_id)
    assert context is not None
    assert context["request"]["id"] == request_id
    assert context["crawl_result"]["request_id"] == request_id
    assert context["summary"]["request_id"] == request_id
    assert (
        await repo.async_get_request_id_by_url_with_summary(43, "https://example.com/context")
        == request_id
    )
    assert list((await repo.async_get_requests_by_ids([request_id], user_id=43)).keys()) == [
        request_id
    ]
    assert [row["id"] for row in await repo.async_get_all_for_user(43)] == [request_id]
