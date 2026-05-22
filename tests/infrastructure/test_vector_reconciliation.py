"""Vector reconciliation contracts."""

from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    Repository,
    RepositoryEmbedding,
    Request,
    Summary,
    SummaryEmbedding,
    User,
)
from app.db.models.repository import RepoSource
from app.db.session import Database
from app.infrastructure.vector.reconciliation import VectorIndexReconciler

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class FakeVectorStore:
    available = True

    def __init__(self, *, summaries: set[int], repositories: set[int]) -> None:
        self._summaries = summaries
        self._repositories = repositories

    def count(self) -> int:
        return len(self._summaries) + len(self._repositories)

    def get_indexed_summary_ids(self, *, limit: int | None = None, user_id: int | None = None):
        return set(self._summaries)

    def get_indexed_repository_ids(self, *, limit: int | None = None, user_id: int | None = None):
        return set(self._repositories)


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
        await session.execute(delete(RepositoryEmbedding))
        await session.execute(delete(Repository))
        await session.execute(delete(SummaryEmbedding))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _seed(database: Database) -> dict[str, int]:
    now = dt.datetime(2026, 5, 22, 12, tzinfo=UTC)
    async with database.transaction() as session:
        user = User(telegram_user_id=1001, username="owner")
        session.add(user)
        request_one = Request(type="url", status="completed", user_id=1001, updated_at=now)
        request_two = Request(type="url", status="completed", user_id=1001, updated_at=now)
        session.add_all([request_one, request_two])
        await session.flush()
        summary_one = Summary(
            request_id=request_one.id,
            lang="en",
            json_payload={"summary_250": "one"},
            updated_at=now,
        )
        summary_two = Summary(
            request_id=request_two.id,
            lang="en",
            json_payload={"summary_250": "two"},
            updated_at=now,
        )
        repo_one = Repository(
            github_id=10_001,
            owner="owner",
            name="repo-one",
            full_name="owner/repo-one",
            url="https://github.com/owner/repo-one",
            analysis_json={"purpose": "test", "tech_stack": [], "architecture_summary": ""},
            source=RepoSource.STARRED,
            is_starred=True,
            user_id=1001,
        )
        session.add_all([summary_one, summary_two, repo_one])
        await session.flush()
        session.add_all(
            [
                SummaryEmbedding(
                    summary_id=summary_one.id,
                    model_name="expected-model",
                    model_version="1.0",
                    embedding_blob=b"one",
                    dimensions=3,
                    last_indexed_at=now,
                    index_status="indexed",
                ),
                SummaryEmbedding(
                    summary_id=summary_two.id,
                    model_name="old-model",
                    model_version="0.9",
                    embedding_blob=b"two",
                    dimensions=3,
                    last_indexed_at=now - dt.timedelta(hours=1),
                    index_status="indexed",
                ),
            ]
        )
        return {
            "summary_one": summary_one.id,
            "summary_two": summary_two.id,
            "repo_one": repo_one.id,
        }


@pytest.mark.asyncio
async def test_reconciliation_detects_missing_and_stale_vectors(database: Database) -> None:
    ids = await _seed(database)
    report = await VectorIndexReconciler(
        database=database,
        vector_store=FakeVectorStore(summaries={ids["summary_one"]}, repositories=set()),
        expected_summary_models={"expected-model"},
        expected_repository_models={"expected-model"},
    ).inspect(now=dt.datetime(2026, 5, 22, 13, tzinfo=UTC))

    assert report.status == "degraded"
    assert report.expected_summaries == 2
    assert report.expected_repositories == 1
    assert report.indexed_points == 1
    assert report.missing_summary_vectors == 1
    assert report.missing_repository_vectors == 1
    assert report.missing_repository_embeddings == 1
    assert report.stale_summary_embeddings == 1
    assert report.stale_embedding_model_count == 1
    assert report.lag_seconds > 0


@pytest.mark.asyncio
async def test_reconciliation_degrades_cleanly_when_vector_store_disabled(
    database: Database,
) -> None:
    await _seed(database)

    report = await VectorIndexReconciler(
        database=database,
        vector_store=None,
        expected_summary_models={"expected-model"},
        expected_repository_models={"expected-model"},
    ).inspect()

    assert report.status == "disabled"
    assert report.vector_store_available is False
    assert report.indexed_points is None
    assert report.missing_summary_vectors == 0
