from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, update

from app.config.database import DatabaseConfig
from app.db.models import CrawlResult, LLMAttemptTrigger, LLMCall, Request, Summary
from app.db.session import Database
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.llm_repository import (
    LLMRepositoryAdapter,
)
from app.tasks.purge_raw_data import _purge_reader_source_content

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
        await session.execute(delete(Summary))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Request))
    try:
        yield db
    finally:
        async with db.transaction() as session:
            await session.execute(delete(LLMCall))
            await session.execute(delete(Summary))
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
async def test_llm_repository_persists_agent_call_without_request(database: Database) -> None:
    """Agent-originated calls (no parent request) persist with request_id NULL.

    Regression guard for the schema/code contract mismatch where
    ``llm_calls.request_id`` was ``NOT NULL`` while ``persist_agent_llm_call``
    writes ``request_id=None`` -- silently dropping every agent LLM call
    (fixed by migration 0051). The unit tests for agents use a fake repo, so
    only a real-schema insert exercises the NOT-NULL constraint.
    """
    repo = LLMRepositoryAdapter(database)

    call_id = await repo.async_insert_llm_call(
        {
            "request_id": None,
            "provider": "openrouter",
            "model": "openrouter/agent-model",
            "endpoint": "signal_judge",
            "status": "success",
            "cost_usd": 0.01,
            "latency_ms": 123,
            "structured_output_used": True,
        }
    )

    assert call_id > 0
    async with database.transaction() as session:
        row = await session.get(LLMCall, call_id)
        assert row is not None
        assert row.request_id is None
        # attempt_trigger may load as the enum member or its value depending on
        # SQLAlchemy config; normalize before comparing.
        trigger = getattr(row.attempt_trigger, "value", row.attempt_trigger)
        assert trigger == LLMAttemptTrigger.agent.value
        assert row.attempt_index == 1
        assert row.endpoint == "signal_judge"


@pytest.mark.asyncio
async def test_crawl_result_repository_is_idempotent(database: Database) -> None:
    request = await _request(database, user_id=1002)
    repo = CrawlResultRepositoryAdapter(database)

    first_id = await repo.async_insert_crawl_result(
        request.id,
        success=True,
        content_text="Normalized article body.",
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
    assert row["content_text"] == "Normalized article body."
    assert row["metadata_json"] == {"source": "test"}
    assert (await repo.async_get_crawl_result_by_id(first_id) or {})["request_id"] == request.id
    assert await repo.async_get_max_server_version(request.user_id or 0) is not None
    rows = await repo.async_get_all_for_user(request.user_id or 0)
    assert [item["id"] for item in rows] == [first_id]


@pytest.mark.asyncio
async def test_crawl_result_repository_backfill_replaces_missing_body(database: Database) -> None:
    request = await _request(database, user_id=1003)
    repo = CrawlResultRepositoryAdapter(database)

    original_id = await repo.async_insert_crawl_result(
        request.id,
        success=True,
        metadata_json={"title": "Original title"},
        source_url=request.normalized_url,
        status="ok",
    )
    upserted_id = await repo.async_upsert_crawl_result(
        request.id,
        success=True,
        content_text="Restored Complete article body.",
        markdown="# Restored\n\nComplete article body.",
        metadata_json={"title": "Restored title"},
        source_url=request.normalized_url,
        status="ok",
        endpoint="scrapling",
        attempt_log=[{"provider": "scrapling", "status": "success"}],
        winning_provider="scrapling",
    )

    assert upserted_id == original_id
    row = await repo.async_get_crawl_result_by_request(request.id)
    assert row is not None
    assert row["content_text"] == "Restored Complete article body."
    assert row["content_markdown"] == "# Restored\n\nComplete article body."
    assert row["metadata_json"] == {"title": "Restored title"}
    assert row["winning_provider"] == "scrapling"
    assert row["attempt_log"] == [{"provider": "scrapling", "status": "success"}]


@pytest.mark.asyncio
async def test_reader_source_retention_uses_artifact_freshness(database: Database) -> None:
    now = datetime.now(UTC)
    request = await _request(database, user_id=1004)
    repo = CrawlResultRepositoryAdapter(database)
    artifact_id = await repo.async_materialize_source_content(
        request.id,
        "Freshly restored legacy article.",
        source_url=request.normalized_url,
        content_source="local:markdown",
    )

    async with database.transaction() as session:
        await session.execute(
            update(Request)
            .where(Request.id == request.id)
            .values(created_at=now - timedelta(days=365))
        )

    assert await _purge_reader_source_content(database, now, days=30, batch=100) == 0
    fresh = await repo.async_get_crawl_result_by_id(artifact_id)
    assert fresh is not None
    assert fresh["content_text"] == "Freshly restored legacy article."

    async with database.transaction() as session:
        await session.execute(
            update(CrawlResult)
            .where(CrawlResult.id == artifact_id)
            .values(updated_at=now - timedelta(days=31))
        )

    assert await _purge_reader_source_content(database, now, days=30, batch=100) == 1
    expired = await repo.async_get_crawl_result_by_id(artifact_id)
    assert expired is not None
    assert expired["content_text"] is None


@pytest.mark.asyncio
async def test_materialize_source_content_preserves_raw_provider_fields(
    database: Database,
) -> None:
    request = await _request(database, user_id=1004)
    repo = CrawlResultRepositoryAdapter(database)
    artifact_id = await repo.async_insert_crawl_result(
        request.id,
        success=True,
        markdown="# Raw provider article",
        html="<h1>Raw provider article</h1>",
        metadata_json={"title": "Original title"},
        source_url=request.normalized_url,
        winning_provider="scrapling",
    )

    materialized_id = await repo.async_materialize_source_content(
        request.id,
        "Normalized article body.",
        source_url=request.normalized_url,
        correlation_id="reconcile-source",
        content_source="local:markdown",
    )

    assert materialized_id == artifact_id
    row = await repo.async_get_crawl_result_by_request(request.id)
    assert row is not None
    assert row["content_text"] == "Normalized article body."
    assert row["content_markdown"] == "# Raw provider article"
    assert row["content_html"] == "<h1>Raw provider article</h1>"
    assert row["metadata_json"] == {"title": "Original title"}
    assert row["winning_provider"] == "scrapling"


@pytest.mark.asyncio
async def test_source_reconcile_scan_finds_only_missing_normalized_content(
    database: Database,
) -> None:
    from app.tasks.reconcile_source_content import (
        _fetch_missing_source_rows,
        _get_missing_source_stats,
    )

    local_request = await _request(database, user_id=1101)
    network_request = await _request(database, user_id=1102)
    durable_request = await _request(database, user_id=1103)
    async with database.transaction() as session:
        await session.execute(
            update(Request)
            .where(Request.id.in_([local_request.id, network_request.id, durable_request.id]))
            .values(status="ok")
        )
        for request in (local_request, network_request, durable_request):
            session.add(
                Summary(
                    request_id=request.id,
                    lang="en",
                    json_payload={"summary_250": "summary"},
                )
            )
        session.add(
            CrawlResult(
                request_id=local_request.id,
                firecrawl_success=True,
                content_markdown="# Locally recoverable",
            )
        )
        session.add(
            CrawlResult(
                request_id=durable_request.id,
                firecrawl_success=True,
                content_text="Already normalized.",
            )
        )

    rows = await _fetch_missing_source_rows(database, limit=10)

    assert {row["request_id"] for row in rows} == {
        local_request.id,
        network_request.id,
    }
    by_request = {row["request_id"]: row for row in rows}
    assert by_request[local_request.id]["has_local_source"] is True
    assert by_request[network_request.id]["has_local_source"] is False
    missing_total, oldest_missing_age_seconds = await _get_missing_source_stats(database)
    assert missing_total == 2
    assert oldest_missing_age_seconds >= 0

    rows_after_cursor = await _fetch_missing_source_rows(
        database,
        limit=10,
        after_summary_id=int(rows[0]["summary_id"]),
    )
    assert [row["summary_id"] for row in rows_after_cursor] == [rows[1]["summary_id"]]
