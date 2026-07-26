from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import Table, select

from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.models import ALL_MODELS, Request, Summary, TopicSearchIndex
from app.db.session import Database
from app.db.topic_search_manager import TopicSearchIndexManager

if TYPE_CHECKING:
    import logging

pytestmark = pytest.mark.postgres


class _Logger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        return None

    def warning(self, *_args: object, **_kwargs: object) -> None:
        return None


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def _all_tables() -> list[Table]:
    return [cast("Table", model.__table__) for model in ALL_MODELS]


@pytest.mark.asyncio
async def test_topic_search_manager_indexes_and_finds_requests() -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres topic search smoke test")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    manager = TopicSearchIndexManager(database, cast("logging.Logger", _Logger()))
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=list(reversed(_all_tables())))
            await connection.run_sync(Base.metadata.create_all, tables=_all_tables())

        async with database.transaction() as session:
            request = Request(
                type="url",
                status="done",
                input_url="https://example.com/ai",
                normalized_url="https://example.com/ai",
                content_text="PostgreSQL full text search for AI summaries",
            )
            session.add(request)
            await session.flush()
            summary = Summary(
                request_id=request.id,
                lang="en",
                json_payload={
                    "title": "Postgres search",
                    "summary_250": "Async SQLAlchemy powers topic discovery",
                    "topic_tags": ["postgres", "search"],
                    "metadata": {"domain": "example.com"},
                },
            )
            session.add(summary)

        await manager.ensure_index()
        found = await manager.find_request_ids("postgres search", candidate_limit=5)

        assert found == [request.id]

        async with database.session() as session:
            indexed = await session.scalar(
                select(TopicSearchIndex).where(TopicSearchIndex.request_id == request.id)
            )
        assert indexed is not None
        assert indexed.tags == "postgres search"
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=list(reversed(_all_tables())))
        await database.dispose()
