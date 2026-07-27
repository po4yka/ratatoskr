"""Regression coverage for durable Reader source content across DB sessions."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.api.routers.content.summaries import get_summary_content
from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
from app.config.database import DatabaseConfig
from app.db.models import CrawlResult, LLMCall, Request, Summary
from app.db.session import Database
from app.domain.models.request import RequestStatus
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.llm_repository import LLMRepositoryAdapter
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
)

pytestmark = pytest.mark.integration

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = os.getenv("TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Reader durability integration tests")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await database.migrate()
    async with database.transaction() as session:
        await session.execute(delete(LLMCall))
        await session.execute(delete(Summary))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Request))
    try:
        yield database
    finally:
        async with database.transaction() as session:
            await session.execute(delete(LLMCall))
            await session.execute(delete(Summary))
            await session.execute(delete(CrawlResult))
            await session.execute(delete(Request))
        await database.dispose()


@pytest.mark.asyncio
async def test_completed_url_keeps_reader_source_after_fresh_session(database: Database) -> None:
    requests = RequestRepositoryAdapter(database)
    crawls = CrawlResultRepositoryAdapter(database)
    summaries = SummaryRepositoryAdapter(database)
    request_id = await requests.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="reader-durability",
        user_id=1402,
        input_url="https://example.com/durable-reader",
        normalized_url="https://example.com/durable-reader",
        dedupe_hash="reader-durability",
    )
    artifact_id = await crawls.async_upsert_crawl_result(
        request_id=request_id,
        success=True,
        content_text="The normalized article survives a completely fresh database session.",
        markdown="# Provider-specific raw payload",
        source_url="https://example.com/durable-reader",
        status="ok",
        winning_provider="integration-test",
    )
    finalized = await summaries.async_persist_summary_with_llm_calls(
        request_id=request_id,
        lang="en",
        json_payload={"summary_250": "Durability regression summary."},
        llm_calls=[],
    )
    assert finalized.summary_id is not None
    assert await requests.async_complete_with_source_artifact(request_id, artifact_id) is True

    # Drop every pooled connection so the Reader read cannot observe in-memory
    # ORM state from the write phase.
    await database.dispose()
    fresh_database = Database(
        DatabaseConfig(dsn=os.environ["TEST_DATABASE_URL"], pool_size=1, max_overflow=1)
    )
    use_case = SummaryReadModelUseCase(
        SummaryRepositoryAdapter(fresh_database),
        RequestRepositoryAdapter(fresh_database),
        CrawlResultRepositoryAdapter(fresh_database),
        LLMRepositoryAdapter(fresh_database),
    )
    try:
        response = await get_summary_content(
            summary_id=finalized.summary_id,
            format="markdown",
            user={"user_id": 1402},
            use_case=use_case,
        )
    finally:
        await fresh_database.dispose()

    content = response["data"]["content"]
    assert content["content"] == (
        "The normalized article survives a completely fresh database session."
    )
    assert content["format"] == "text"
    assert content["contentType"] == "text/plain"
