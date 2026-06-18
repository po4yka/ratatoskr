from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import CrawlResult, LLMCall, Request
from app.db.session import Database
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.llm_repository import (
    LLMRepositoryAdapter,
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
        await session.execute(delete(LLMCall))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Request))
    try:
        yield db
    finally:
        async with db.transaction() as session:
            await session.execute(delete(LLMCall))
            await session.execute(delete(CrawlResult))
            await session.execute(delete(Request))
        await db.dispose()


async def _request(database: Database, *, user_id: int = 1001) -> Request:
    async with database.transaction() as session:
        request = Request(
            type="url",
            status="processing",
            correlation_id="repo-core",
            user_id=user_id,
            input_url="https://example.com/repo-core",
            normalized_url="https://example.com/repo-core",
            dedupe_hash=f"repo-core-{user_id}",
        )
        session.add(request)
        await session.flush()
        return request


@pytest.mark.asyncio
async def test_llm_repository_persists_and_reads_batch(database: Database) -> None:
    request = await _request(database)
    repo = LLMRepositoryAdapter(database)

    inserted_ids = await repo.async_insert_llm_calls_batch(
        [
            {
                "request_id": request.id,
                "provider": "openrouter",
                "model": "model-a",
                "status": "ok",
                "response_text": "first",
                "response_json": {"ok": True},
                "fallback_model_used": None,
                "retry_exhausted": False,
                "total_latency_ms": 75,
            },
            {
                "request_id": request.id,
                "provider": "internal",
                "model": "model-b",
                "status": "error",
                "error_text": "failed",
                "error_context_json": {"reason": "test"},
                "response_text": "",
                "retry_exhausted": True,
                "total_latency_ms": 125,
            },
        ]
    )

    assert len(inserted_ids) == 2
    assert await repo.async_count_llm_calls_by_request(request.id) == 2
    assert await repo.async_get_latest_llm_model_by_request_id(request.id) == "model-b"
    latest_error = await repo.async_get_latest_error_by_request(request.id)
    assert latest_error is not None
    assert latest_error["error_context_json"] == {"reason": "test"}
    assert latest_error["retry_exhausted"] is True
    assert latest_error["total_latency_ms"] == 125
    rows = await repo.async_get_all_for_user(request.user_id or 0)
    assert [row["id"] for row in rows] == inserted_ids
    assert rows[0]["fallback_model_used"] is None
    assert rows[0]["retry_exhausted"] is False
    assert rows[0]["total_latency_ms"] == 75


@pytest.mark.asyncio
async def test_crawl_result_repository_is_idempotent(database: Database) -> None:
    request = await _request(database, user_id=1002)
    repo = CrawlResultRepositoryAdapter(database)

    first_id = await repo.async_insert_crawl_result(
        request.id,
        success=True,
        markdown="# Title",
        metadata_json={"source": "test"},
        source_url=request.normalized_url,
        status="ok",
        latency_ms=42,
    )
    second_id = await repo.async_insert_crawl_result(request.id, success=True)

    assert second_id == first_id
    row = await repo.async_get_crawl_result_by_request(request.id)
    assert row is not None
    assert row["request_id"] == request.id
    assert row["metadata_json"] == {"source": "test"}
    assert await repo.async_get_max_server_version(request.user_id or 0) is not None
    rows = await repo.async_get_all_for_user(request.user_id or 0)
    assert [item["id"] for item in rows] == [first_id]
