"""Postgres-backed tests for the embedding repository."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import Request, Summary, SummaryEmbedding
from app.db.session import Database
from app.infrastructure.persistence.repositories.embedding_repository import (
    EmbeddingRepositoryAdapter,
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
        await session.execute(delete(SummaryEmbedding))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))


async def _summary(database: Database, *, suffix: str) -> tuple[Request, Summary]:
    async with database.transaction() as session:
        request = Request(
            type="url",
            status="completed",
            correlation_id=f"embedding-{suffix}",
            user_id=12001,
            input_url=f"https://example.com/embedding/{suffix}",
            normalized_url=f"https://example.com/embedding/{suffix}",
            dedupe_hash=f"embedding-{suffix}",
        )
        session.add(request)
        await session.flush()
        summary = Summary(
            request_id=request.id,
            lang="en",
            json_payload={"summary_250": suffix},
        )
        session.add(summary)
        await session.flush()
        return request, summary


@pytest.mark.asyncio
async def test_embedding_repository_upserts_and_reads(database: Database) -> None:
    repo = EmbeddingRepositoryAdapter(database)
    request, summary = await _summary(database, suffix="first")

    await repo.async_create_or_update_summary_embedding(
        summary.id,
        b"first",
        "model-a",
        "v1",
        3,
        language="en",
    )
    await repo.async_create_or_update_summary_embedding(
        summary.id,
        b"second",
        "model-b",
        "v2",
        4,
        language="ru",
    )

    embedding = await repo.async_get_summary_embedding(summary.id)
    assert embedding is not None
    assert embedding["embedding_blob"] == b"second"
    assert embedding["model_name"] == "model-b"
    assert embedding["dimensions"] == 4
    assert embedding["language"] == "ru"

    rows = await repo.async_get_embeddings_by_request_ids([request.id])
    assert rows == [
        {
            "request_id": request.id,
            "summary_id": summary.id,
            "embedding_blob": b"second",
            "json_payload": {"summary_250": "first"},
            "normalized_url": request.normalized_url,
            "input_url": request.input_url,
        }
    ]


@pytest.mark.asyncio
async def test_mark_indexed_uses_content_hash_compare_and_swap(database: Database) -> None:
    repo = EmbeddingRepositoryAdapter(database)
    _request, summary = await _summary(database, suffix="cas")
    _other_request, other_summary = await _summary(database, suffix="cas-other")
    await repo.async_create_or_update_summary_embedding(
        summary.id,
        b"vector-v1",
        "model-a",
        "v1",
        1,
        content_hash="content-v1",
    )
    await repo.async_create_or_update_summary_embedding(
        other_summary.id,
        b"vector-v2",
        "model-a",
        "v1",
        1,
        content_hash="content-v2",
    )

    assert await repo.async_mark_summary_embeddings_indexed(
        {summary.id: "content-v0", other_summary.id: "content-v2"}
    ) == [other_summary.id]
    pending = await repo.async_get_summary_embedding(summary.id)
    assert pending is not None
    assert pending["index_status"] == "pending"
    assert pending["last_indexed_at"] is None

    assert await repo.async_mark_summary_embeddings_indexed({summary.id: "content-v1"}) == [
        summary.id
    ]
    indexed = await repo.async_get_summary_embedding(summary.id)
    assert indexed is not None
    assert indexed["index_status"] == "indexed"
    assert indexed["last_indexed_at"] is not None


@pytest.mark.asyncio
async def test_embedding_repository_lists_all_and_recent(database: Database) -> None:
    repo = EmbeddingRepositoryAdapter(database)
    first_request, first_summary = await _summary(database, suffix="one")
    second_request, second_summary = await _summary(database, suffix="two")
    await repo.async_create_or_update_summary_embedding(first_summary.id, b"one", "m", "v", 1)
    await repo.async_create_or_update_summary_embedding(second_summary.id, b"two", "m", "v", 1)

    assert [row["request_id"] for row in await repo.async_get_all_embeddings()] == [
        first_request.id,
        second_request.id,
    ]
    assert [row["request_id"] for row in await repo.async_get_recent_embeddings(limit=1)] == [
        second_request.id
    ]
    assert await repo.async_get_recent_embeddings(limit=0) == []
    assert await repo.async_get_embeddings_by_request_ids([]) == []
